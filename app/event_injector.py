"""Event injector: overlays synthetic extreme events onto the reading
stream at read time, for the four demo scenarios in PROJECT_BRIEF.md.

Strict separation, per the brief: this module only ever produces a NEW,
modified copy of the readings — it never mutates the cached/stored dataset,
and it never touches RiskAssessment or assess_risk's rules. assess_risk
receives the overlaid readings and evaluates them with its own unchanged
logic, exactly as if they were real. That's what keeps a "confirmed flood"
or "possible fault" produced by an injected scenario just as honest as one
produced by real data — the rules can't tell the difference, and don't try
to. Reset is trivial because of this: clearing `active_events` leaves the
underlying dataset untouched, so the very next tick is a clean replay.

Each active event applies one or more "deltas" — (sensor_id, variable) pairs
with a smooth rise/hold/decay envelope — rather than a single-frame square
wave. A discontinuity would look artificial and would also interact oddly
with the rate-of-rise layer, which specifically looks at a 3-timestep
window; a smooth ramp is what a real (if fast) hydrological response would
actually look like.
"""

from dataclasses import dataclass, field

import pandas as pd

from risk_assessment import CUMULATIVE_LAG_FROM_S01, LAG_MARGIN

CHANNEL_SENSORS = ["S01", "S02", "S03", "S04"]
UPSTREAM_SENSOR_ID = "S01"

# Dropdown order, and SCENARIOS[0] is the default selection. Ordered
# catchment-wide first: it is the only scenario that escalates the overall
# state, so it is the one worth landing on by default.
SCENARIOS = [
    "Catchment-wide event",
    "Convective storm",
    "Saturated antecedent",
    "Sensor fault",
]

# "Catchment-wide event" hits every sensor at once (per its own propagation
# lags) and has no single target; the other three scenarios apply to one
# chosen sensor. The Dash injector panel uses this to show/hide its
# target-sensor control per scenario, rather than hardcoding the scenario
# names a second time.
SCENARIOS_NEEDING_TARGET = {"Convective storm", "Sensor fault", "Saturated antecedent"}

# Targets a scenario must not offer, because it cannot demonstrate what the
# scenario exists to show there.
#
# S01 is excluded from "Sensor fault" for a structural reason, not a tuning
# one: it is the upstream boundary gauge, so it self-confirms on
# (own_rain OR rising_fast). The suppression zeroes its rainfall, but a
# fault's whole premise is a sudden water-level jump, which makes
# rising_fast unavoidably true — so a fault injected at S01 always reads as
# a confirmed flood, never a fault. That is CORRECT per PROJECT_BRIEF.md (a gauge
# with nothing upstream of it has nothing to contradict a spurious jump);
# it just means the scenario can't do its job there, so it isn't offered.
SCENARIO_EXCLUDED_TARGETS = {"Sensor fault": {UPSTREAM_SENSOR_ID}}


def targets_for_scenario(scenario: str, sensor_ids: list) -> list:
    """The sensor ids this scenario may be aimed at, in the order given."""
    excluded = SCENARIO_EXCLUDED_TARGETS.get(scenario, set())
    return [sid for sid in sensor_ids if sid not in excluded]

# Shown under the scenario picker. Kept to the mechanism + the gotcha, not a
# restatement of the scenario name the dropdown already shows.
#
# Plain text, no markdown: main.py renders this straight into an html.Div's
# children, not a dcc.Markdown, so any ** would print as literal asterisks.
# Keyed by name, so this mapping is independent of SCENARIOS' order.
SCENARIO_DESCRIPTIONS = {
    "Catchment-wide event": (
        "Rain and water-level rise at all four sensors, each shifted by its "
        "real propagation lag from S01 (9 / 12 / 15 steps). Sensors confirm "
        "in turn as the wave travels downstream: the one scenario that "
        "escalates the overall catchment state."
    ),
    "Convective storm": (
        "Rain and a matching water-level rise at the target sensor only. S01 "
        "confirms on its own rain; S02-S04 confirm against S01's rain "
        "instead, so a storm seen only downstream reads as a possible "
        "fault, since from S01 it's indistinguishable from a stuck sensor."
    ),
    "Saturated antecedent": (
        "Same shape as a convective storm, but a higher runoff coefficient "
        "(wet ground sheds rain rather than absorbing it) drives a larger "
        "rise from less rain. The same S01 self-confirmation caveat applies "
        "downstream."
    ),
    "Sensor fault": (
        "Water level jumps at the target sensor while rainfall is suppressed "
        "there and at S01: no rain anywhere to confirm it. Always reads as "
        "a possible fault; the catchment state never escalates."
    ),
}

# cm of water_level rise generated per mm/h of injected rainfall. This is the
# injector's own simplified rainfall->runoff model — assess_risk has no
# concept of this conversion (or of soil moisture at all); it only ever sees
# the resulting water_level/rainfall_intensity readings, exactly like it
# would from Ano's real simulation. SATURATED is higher: the same rain
# produces a bigger, faster rise when the ground is already wet and can't
# absorb it.
#
# Tuned against the CURRENT synthetic data, whose water_level baseline is
# still near-zero (Ano's rescale to realistic 40-50cm-dry Botič values
# hasn't landed yet — see PROJECT_BRIEF.md). These coefficients push the default
# magnitudes across Watch/Alert regardless of that baseline; once the
# corrected data lands this may need retuning down.
RUNOFF_CM_PER_MM_H = 9.5
SATURATED_RUNOFF_CM_PER_MM_H = 17.0

# Percentage points added to soil_moisture for the saturated-antecedent
# scenario. That scenario's premise is wet GROUND, not just heavier runoff:
# without this it raised the runoff coefficient while leaving the soil
# moisture reading untouched, so the dashboard showed an amplified rise
# sitting next to a soil-moisture tile that still read dry — the one number
# a viewer would check to see WHY the rise was amplified contradicted it.
#
# +30 points against this dataset (min 0.3, mean 29, p95 59) lands a typical
# moment convincingly wet without running past 100% on an already-damp one;
# apply_injections does no clamping, so the headroom has to come from the
# size of the step.
SATURATED_SOIL_MOISTURE_RISE_PCT = 30.0

# The reserved id carrying the single catchment-wide soil_moisture series in
# the target data shape (PROJECT_BRIEF.md's data contract).
CATCHMENT_SENSOR_ID = "CATCHMENT"

# Fixed magnitude per scenario — pre-tuned to reliably cross the stage each
# scenario is meant to demonstrate (see the coefficients above). No slider:
# one less control to explain, and the point is showing the rule engine
# react to a named scenario, not sweeping arbitrary storm sizes.
DEFAULT_MAGNITUDE = {
    "Convective storm": 15.0,       # mm/h peak rainfall
    "Catchment-wide event": 30.0,   # mm/h peak rainfall
    "Sensor fault": 150.0,          # cm water_level jump
    "Saturated antecedent": 10.0,   # mm/h peak rainfall
}


@dataclass
class Delta:
    sensor_id: str
    variable: str
    peak: float
    rise: int
    hold: int
    decay: int
    lag_offset: int = 0
    mode: str = "add"  # "add": value += peak*factor.  "suppress": value *= (1-factor).


@dataclass
class InjectedEvent:
    scenario: str
    target_sensor: str | None
    magnitude: float
    trigger_step: int
    deltas: list = field(default_factory=list)

    @property
    def duration(self) -> int:
        return max((d.rise + d.hold + d.decay + d.lag_offset for d in self.deltas), default=0)


def _pulse(relative_step: int, rise: int, hold: int, decay: int) -> float:
    """0 -> 1 -> 0 smooth envelope: ramps up over `rise` steps, holds at 1
    for `hold` steps, then ramps back down over `decay` steps. Never a
    single-frame jump."""
    if relative_step < 0:
        return 0.0
    if relative_step < rise:
        return (relative_step + 1) / rise
    if relative_step < rise + hold:
        return 1.0
    decay_idx = relative_step - rise - hold
    if decay_idx < decay:
        return max(0.0, 1.0 - (decay_idx + 1) / decay)
    return 0.0


def _delta_step_span(delta: Delta) -> tuple[int, int] | None:
    """The relative-step range [first, last] over which this delta's pulse is
    actually nonzero, or None if it never is.

    Derived by evaluating `_pulse` rather than deriving a closed form from
    rise/hold/decay, deliberately: apply_injections marks a row `injected`
    exactly when `factor > 0`, so running the same function with the same
    test is what guarantees a highlight band can never disagree with the
    readings it is supposed to be highlighting. A closed form would be a
    second, independently-maintained description of the envelope's edges —
    note that the last decay step already evaluates to exactly 0.0, which is
    the kind of off-by-one this sidesteps entirely.
    """
    horizon = delta.rise + delta.hold + delta.decay
    nonzero = [r for r in range(horizon + 1) if _pulse(r, delta.rise, delta.hold, delta.decay) > 0]
    if not nonzero:
        return None
    return nonzero[0], nonzero[-1]


def injected_spans(events: list, timeline: list, sensor_id: str, variable: str) -> list[tuple]:
    """Timestamp ranges [(start, end), ...] this sensor/variable is under
    injection for, across every active event. Overlapping ranges are merged
    so two events can't stack their translucent bands into a darker one.

    Spans cover each event's WHOLE envelope, including steps the replay has
    not reached yet. That is what lets the highlight band work as a layout
    shape under the extendData pattern: a shape cannot be appended to the way
    trace data can, so the band is drawn once at trigger time and the sliding
    x-window (main.chart_x_range, which always ends at "now") does the rest —
    it clips whatever is still in the future, and reveals more of the band on
    its own as the window advances. No per-tick layout write, and nothing
    about the future is shown early.

    Each range is padded by half a sampling interval either side so a bar or
    point sitting exactly on the first/last injected timestamp is enclosed by
    the band rather than sitting on its edge.
    """
    if not events or not timeline:
        return []

    last_step = len(timeline) - 1
    half_interval = (timeline[1] - timeline[0]) / 2 if len(timeline) > 1 else None

    step_ranges: list[tuple[int, int]] = []
    for event in events:
        for delta in event.deltas:
            if delta.sensor_id != sensor_id or delta.variable != variable:
                continue
            span = _delta_step_span(delta)
            if span is None:
                continue
            start = event.trigger_step + delta.lag_offset + span[0]
            end = event.trigger_step + delta.lag_offset + span[1]
            # A wraparound clears events outright, so a span can only ever
            # run off the END of the timeline, never before its start.
            if start > last_step:
                continue
            step_ranges.append((max(start, 0), min(end, last_step)))

    if not step_ranges:
        return []

    merged: list[list[int]] = []
    for start, end in sorted(step_ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    spans = []
    for start, end in merged:
        first, last = timeline[start], timeline[end]
        if half_interval is not None:
            first, last = first - half_interval, last + half_interval
        spans.append((first, last))
    return spans


def build_event(scenario: str, target_sensor: str | None, magnitude: float, trigger_step: int) -> InjectedEvent:
    """Construct the delta list for one triggered scenario. Pure — reads no
    session_state, touches no data.

    An excluded target is corrected here rather than trusted from the caller,
    so SCENARIO_EXCLUDED_TARGETS holds by CONSTRUCTION and not merely by the
    UI happening to offer the right options. The dropdown filters its options
    from the same rule, but the Trigger callback reads the target as `State`:
    switching to a scenario and triggering it inside the same ~50ms, before
    the dropdown-correction callback lands, would otherwise still hand an
    excluded target straight through. That is not hypothetical — for
    "Sensor fault" it reintroduces exactly the always-confirms outcome the
    exclusion exists to prevent.

    Driven off the shared rule (never a second hardcoded S01 check), so the
    two layers cannot drift if the exclusion list ever changes. Scenarios
    with no exclusions are unaffected, and a None target — a scenario that
    has no single target at all — is left alone.
    """
    allowed_targets = targets_for_scenario(scenario, CHANNEL_SENSORS)
    if target_sensor is not None and target_sensor not in allowed_targets:
        target_sensor = allowed_targets[0]

    if scenario == "Convective storm":
        # Sharp, brief rainfall spike at one sensor, with the water_level
        # response it would locally cause. Only self-confirms cleanly at
        # S01 (see render_sidebar's caption) — a downstream target with no
        # upstream rain will surface as a possible_fault instead, which is
        # itself an honest, correct answer under this system's rules.
        rise, hold, decay = 2, 1, 3
        deltas = [
            Delta(target_sensor, "rainfall_intensity", magnitude, rise, hold, decay),
            Delta(target_sensor, "water_level", magnitude * RUNOFF_CM_PER_MM_H, rise, hold, decay),
        ]

    elif scenario == "Catchment-wide event":
        # One storm sweeping the whole basin: every sensor gets rainfall and
        # a matching water_level response, each shifted by its own
        # cumulative propagation lag from S01 (the same lag table
        # assess_risk's Layer 3 uses) so downstream confirmation happens
        # naturally, not because the injector special-cased it.
        rise, hold, decay = 3, 4, 5
        deltas = []
        for sid in CHANNEL_SENSORS:
            lag = 0 if sid == UPSTREAM_SENSOR_ID else CUMULATIVE_LAG_FROM_S01[sid]
            deltas.append(Delta(sid, "rainfall_intensity", magnitude, rise, hold, decay, lag_offset=lag))
            deltas.append(Delta(sid, "water_level", magnitude * RUNOFF_CM_PER_MM_H, rise, hold, decay, lag_offset=lag))

    elif scenario == "Sensor fault":
        # Water level jumps with NO rainfall anywhere — that "anywhere" is
        # the scenario's own definition, so this deliberately suppresses
        # (rather than just ignores) any real background rainfall at the
        # target and at S01 for the event's duration, guaranteeing the
        # precondition regardless of what the underlying replay happens to
        # be doing when this is triggered.
        rise, hold, decay = 2, 3, 2
        deltas = [Delta(target_sensor, "water_level", magnitude, rise, hold, decay)]
        # The suppression has to reach BACKWARD from the trigger, not just
        # forward. A downstream sensor's Layer-3 confirmation reads S01's
        # rainfall over [now-(lag+margin), now-(lag-margin)] — for S02 that
        # is r-11..r-2 relative to the trigger, entirely BEFORE it. The
        # original forward-only envelope covered r0..r5, so the two ranges
        # were completely disjoint: the suppression never touched the window
        # the confirmation actually reads, and ambient pre-trigger rain at
        # S01 confirmed the jump. Measured across the replay, that produced
        # confirmed_flood on ~21% of trigger positions instead of the
        # possible_fault this scenario guarantees.
        #
        # rise=1/decay=1 makes the pulse a flat 1.0 across the whole span:
        # full zeroing with no partial edges, so there is no step where
        # rainfall is merely halved and could still clear the 2 mm/h bar.
        lag = 0 if target_sensor == UPSTREAM_SENSOR_ID else CUMULATIVE_LAG_FROM_S01[target_sensor]
        back = lag + LAG_MARGIN
        span = back + rise + hold + decay
        suppress_targets = {target_sensor, UPSTREAM_SENSOR_ID}
        for sid in suppress_targets:
            deltas.append(
                Delta(sid, "rainfall_intensity", 0.0, 1, span, 1, mode="suppress", lag_offset=-back)
            )

    elif scenario == "Saturated antecedent":
        # Same shape as a convective storm, but with the saturated-ground
        # runoff coefficient, so a moderate rain produces a bigger, faster
        # water_level rise than the same rain would on dry ground.
        rise, hold, decay = 3, 5, 6
        deltas = [
            Delta(target_sensor, "rainfall_intensity", magnitude, rise, hold, decay),
            Delta(target_sensor, "water_level", magnitude * SATURATED_RUNOFF_CM_PER_MM_H, rise, hold, decay),
        ]
        # The wet ground itself. ANTECEDENT means the catchment was already
        # saturated when the rain arrived, so this envelope is deliberately
        # NOT the rainfall's: rise=1 puts soil moisture at full height on the
        # event's very first step (rather than climbing alongside the rain,
        # which would read as the ground wetting UP during the storm), the
        # hold covers the whole rainfall envelope, and a long decay reflects
        # soil draining far more slowly than a stream crests — the
        # "slow-decaying antecedent wetness" PROJECT_BRIEF.md describes.
        #
        # Emitted against BOTH the target sensor and CATCHMENT because the
        # data contract is mid-migration: soil_moisture is per-sensor in the
        # current file and moves to a single CATCHMENT series in Ano's
        # revision. apply_injections skips a delta whose (sensor, variable)
        # matches no rows, so whichever shape is loaded, one of these applies
        # and the other costs nothing.
        for soil_sensor in (target_sensor, CATCHMENT_SENSOR_ID):
            deltas.append(
                Delta(soil_sensor, "soil_moisture", SATURATED_SOIL_MOISTURE_RISE_PCT, 1, 12, 8)
            )

    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    return InjectedEvent(scenario, target_sensor, magnitude, trigger_step, deltas)


def _still_active(event: InjectedEvent, sim_step: int) -> bool:
    return sim_step - event.trigger_step <= event.duration


def steps_remaining(event: InjectedEvent, sim_step: int) -> int:
    """Steps left in the event's rise/hold/decay envelope, 0 once it has
    fully played out.

    Purely for the injector's status readout. It is NOT an expiry test: the
    dashboard retains events past 0 so their spike stays in the chart's
    history until Reset (see main.injected_readings).
    """
    return max(event.duration - (sim_step - event.trigger_step), 0)


def apply_injections(
    df: pd.DataFrame, timeline: list, active_events: list, sim_step: int
) -> tuple[pd.DataFrame, list]:
    """Return (readings with every active event's pulses blended in — plus an
    'injected' bool column marking every row any event touched, the pruned
    list of events still active after this step). `df` itself is never
    modified. `active_events` and `sim_step` are passed in rather than read
    from st.session_state (Dash has no session_state); the caller owns a
    dcc.Store holding active_events and is responsible for writing the
    returned (pruned) list back into it."""
    result = df.copy()
    result["injected"] = False

    if not active_events:
        return result, []
    if df.empty:
        return result, active_events

    step_of = {t: i for i, t in enumerate(timeline)}
    steps = result["timestamp"].map(step_of)

    still_active = []
    for event in active_events:
        for delta in event.deltas:
            relative = steps - event.trigger_step - delta.lag_offset
            factor = relative.apply(lambda r: _pulse(int(r), delta.rise, delta.hold, delta.decay))
            active_mask = (
                (result["sensor_id"] == delta.sensor_id)
                & (result["variable"] == delta.variable)
                & (factor > 0)
            )
            if not active_mask.any():
                continue
            if delta.mode == "suppress":
                result.loc[active_mask, "value"] = result.loc[active_mask, "value"] * (1 - factor[active_mask])
            else:
                result.loc[active_mask, "value"] = result.loc[active_mask, "value"] + factor[active_mask] * delta.peak
            result.loc[active_mask, "injected"] = True

        if _still_active(event, sim_step):
            still_active.append(event)

    return result, still_active


# --- dcc.Store (de)serialization ------------------------------------------
#
# InjectedEvent/Delta are dataclasses, not natively JSON-serializable — but
# a dcc.Store's `data` prop has to round-trip through JSON to the browser
# and back (Dash has no session_state to just hold live Python objects in).
# These are the sole boundary where that conversion happens; apply_injections
# and build_event keep working with real InjectedEvent/Delta objects and
# don't need to know their caller persists them as plain dicts.


def event_to_dict(event: InjectedEvent) -> dict:
    return {
        "scenario": event.scenario,
        "target_sensor": event.target_sensor,
        "magnitude": event.magnitude,
        "trigger_step": event.trigger_step,
        "deltas": [
            {
                "sensor_id": d.sensor_id,
                "variable": d.variable,
                "peak": d.peak,
                "rise": d.rise,
                "hold": d.hold,
                "decay": d.decay,
                "lag_offset": d.lag_offset,
                "mode": d.mode,
            }
            for d in event.deltas
        ],
    }


def event_from_dict(data: dict) -> InjectedEvent:
    deltas = [Delta(**delta) for delta in data["deltas"]]
    return InjectedEvent(
        scenario=data["scenario"],
        target_sensor=data["target_sensor"],
        magnitude=data["magnitude"],
        trigger_step=data["trigger_step"],
        deltas=deltas,
    )


def events_from_store(raw_events: list) -> list[InjectedEvent]:
    """dcc.Store's raw `data` (list of dicts) -> InjectedEvent objects."""
    return [event_from_dict(e) for e in raw_events]


def events_to_store(events: list[InjectedEvent]) -> list[dict]:
    """InjectedEvent objects -> plain dicts, ready for a dcc.Store's `data`."""
    return [event_to_dict(e) for e in events]


def events_signature(events: list[InjectedEvent]) -> tuple:
    """Stable, order-independent fingerprint of an active-events list. Used
    by the chart callback to tell "the active set actually changed" (a new
    trigger, an expiry) apart from "the same events, re-serialized this
    tick" — active_events round-trips through events_to_store/
    events_from_store every tick regardless of whether anything changed, so
    object identity can't be used to detect a real change."""
    return tuple(sorted((e.trigger_step, e.scenario, e.target_sensor or "") for e in events))


# render_sidebar_controls / render_sidebar_status / init_injector_state were
# Streamlit sidebar renderers (st.sidebar.*, st.session_state) and are not
# ported here — the injector UI is Dash callbacks in main.py, driven by
# dcc.Store("active-events-store") + dcc.Store("sim-step-store").
