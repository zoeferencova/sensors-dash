"""Event injector: overlays synthetic extreme events onto the reading
stream at read time, for the four demo scenarios in CLAUDE.md.

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

from risk_assessment import CUMULATIVE_LAG_FROM_S01

CHANNEL_SENSORS = ["S01", "S02", "S03", "S04"]
UPSTREAM_SENSOR_ID = "S01"

SCENARIOS = [
    "Convective storm",
    "Catchment-wide event",
    "Sensor fault",
    "Saturated antecedent",
]

# Shown under the scenario picker. Kept to the mechanism + the gotcha, not a
# restatement of the scenario name the dropdown already shows.
SCENARIO_DESCRIPTIONS = {
    "Convective storm": (
        "Rainfall + a matching water_level rise at the target sensor only. "
        "Confirms cleanly at S01 (it self-checks its own rain). At S02-S04, "
        "confirmation checks S01's rainfall instead — so a storm confined to "
        "a downstream sensor with nothing seen at S01 reads as a **possible "
        "fault**, not a Watch. That's not a bug: a real storm that localized "
        "would look identical to a stuck sensor from S01's vantage point."
    ),
    "Catchment-wide event": (
        "Rainfall + water_level at all four sensors at once, each shifted by "
        "its real propagation lag from S01 (9/12/15 steps). Every sensor "
        "confirms in turn as the wave arrives, so this is the one scenario "
        "built to escalate the overall catchment state, not just one pin."
    ),
    "Sensor fault": (
        "Water_level jumps at the target sensor with rainfall actively "
        "suppressed at that sensor and at S01 — guaranteeing 'no rainfall "
        "anywhere' regardless of what the replay happens to be doing. Always "
        "reads as a **possible fault**; the catchment state never escalates. "
        "This is the false-alarm-prevention case."
    ),
    "Saturated antecedent": (
        "Same shape as a Convective storm, but the rain-to-water_level "
        "conversion uses a much higher runoff coefficient (wet ground sheds "
        "rain instead of absorbing it), so it climbs further on less rain. "
        "Same S01-only self-confirmation caveat as Convective storm applies "
        "to downstream targets."
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
# hasn't landed yet — see CLAUDE.md). These coefficients push the default
# magnitudes across Watch/Alert regardless of that baseline; once the
# corrected data lands this may need retuning down.
RUNOFF_CM_PER_MM_H = 9.5
SATURATED_RUNOFF_CM_PER_MM_H = 17.0

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


def build_event(scenario: str, target_sensor: str | None, magnitude: float, trigger_step: int) -> InjectedEvent:
    """Construct the delta list for one triggered scenario. Pure — reads no
    session_state, touches no data."""
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
        suppress_targets = {target_sensor, UPSTREAM_SENSOR_ID}
        for sid in suppress_targets:
            deltas.append(Delta(sid, "rainfall_intensity", 0.0, rise, hold, decay, mode="suppress"))

    elif scenario == "Saturated antecedent":
        # Same shape as a convective storm, but with the saturated-ground
        # runoff coefficient, so a moderate rain produces a bigger, faster
        # water_level rise than the same rain would on dry ground.
        rise, hold, decay = 3, 5, 6
        deltas = [
            Delta(target_sensor, "rainfall_intensity", magnitude, rise, hold, decay),
            Delta(target_sensor, "water_level", magnitude * SATURATED_RUNOFF_CM_PER_MM_H, rise, hold, decay),
        ]

    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    return InjectedEvent(scenario, target_sensor, magnitude, trigger_step, deltas)


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

        if sim_step - event.trigger_step <= event.duration:
            still_active.append(event)

    return result, still_active


# render_sidebar_controls / render_sidebar_status / init_injector_state were
# Streamlit sidebar renderers (st.sidebar.*, st.session_state) and are not
# ported here — the injector UI gets rebuilt as Dash callbacks in a later
# step, driven by a dcc.Store("active_events") + dcc.Store("sim_step").
