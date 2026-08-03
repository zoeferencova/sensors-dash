"""Alert / nowcasting logic for the Botič flood dashboard.

Implements the five-layer risk assessment from CLAUDE.md ("The alert /
nowcasting logic"). `assess_risk` is the single function that computes the
full assessment from the system state at the current moment — the
dashboard, map colors, and rule-evaluation panel all read from its output;
none of them re-derive risk themselves. The event injector (built later)
feeds *inputs* into `readings_up_to_now`; it never sets the risk state
directly. That separation is what keeps the demo honest.

Data assumption (per CLAUDE.md): this module consumes clean, reshaped
per-sensor series (via data_loader.get_series). It assumes no NA/invalid
values — a separate load/clean layer guarantees that upstream, so this
logic doesn't re-check validity. "Fault" here means a physically
implausible-but-present reading (high water, no rainfall cause) — NOT
missing data.

NOT wired into the dashboard yet — this module is logic only.
"""

from dataclasses import dataclass

import pandas as pd

from constants import STAGES, stage_for_water_level
from data_loader import get_series

# --- Tunable constants (named exactly as in CLAUDE.md) --------------------

RATE_OF_RISE_CM_PER_MIN = 0.5
"""Layer 2. The 2013 flood rose ~1 cm/min; this is set at roughly half that
so a dangerous rise is caught before reaching that extreme."""

RATE_OF_RISE_WINDOW_TIMESTEPS = 3  # 15 min of history, at the data's 5-min cadence
TIMESTEP_MINUTES = 5               # data contract: fixed 5-minute sampling interval

LAG_MARGIN = 3
"""Layer 3. Flood-wave travel time varies with flow conditions, so upstream
rainfall is searched in a window centered on the point-estimate lag
(±15 min) rather than requiring an exact match — this avoids missing real
events when water moves faster/slower than the estimate."""

RAINFALL_CONFIRM_MM_H = 2.0
"""Layer 3. "Meaningful" upstream rainfall for confirming that a downstream
high reading is a real flood signal rather than an unexplained fault."""

# Cumulative propagation lag from S01, in 5-min timesteps (CLAUDE.md network design).
CUMULATIVE_LAG_FROM_S01 = {
    "S02": 9,
    "S03": 12,
    "S04": 15,
}

UPSTREAM_SENSOR_ID = "S01"
"""S01 is the sole rainfall reference used to confirm the three downstream
sensors: the network's rainfall is one shared catchment series recorded
(with lag) at each sensor, so S01 carries the earliest signal of a storm
that will later show up as a stage rise downstream."""

# ČHMÚ stage bands (cm) live in constants.THRESHOLDS: Watch 120 / Alert 160 /
# Danger 220 / Extreme 306. Repeated here only as a comment for visibility.


@dataclass
class SensorAssessment:
    """Per-sensor result. `conditions` carries the raw booleans the
    rule-evaluation panel needs — the assessment never collapses these away
    into just a final label."""

    sensor_id: str
    latest_timestamp: pd.Timestamp
    latest_water_level: float

    threshold_state: str            # Layer 1: raw ČHMÚ stage, before any confirmation
    rising_fast: bool               # Layer 2: sustained rate-of-rise flag
    upstream_rain_confirmed: bool   # Layer 3: upstream rain found in the lag window
                                     # (S01 has no upstream neighbour — see _assess_sensor)
    confirmed_flood: bool           # Layer 3: threshold_state stands as a real signal
    possible_fault: bool            # Layer 4: negation of Layer 3 — high water, no cause
    effective_state: str            # Feeds Layer 5: threshold_state if confirmed, else "Normal"

    conditions: dict


@dataclass
class RiskAssessment:
    """Top-level output. `overall_state` is the only thing the dashboard's
    headline state should read; per-sensor detail is here for the map and
    the rule-evaluation panel."""

    overall_state: str
    sensors: dict  # sensor_id -> SensorAssessment


def _lag_window_bounds(
    reference_time: pd.Timestamp, lag_timesteps: int, margin_timesteps: int
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(start, end) timestamps for a Layer 3 lookback window: centered on
    `reference_time - lag`, spanning ±margin, and clipped so it never looks
    past `reference_time` (the current simulated moment — no peeking at the
    future). For S01's own-rainfall check (lag=0) this clipping naturally
    reduces the window to "the last `margin` timesteps up to now"."""
    lag_delta = pd.Timedelta(minutes=lag_timesteps * TIMESTEP_MINUTES)
    margin_delta = pd.Timedelta(minutes=margin_timesteps * TIMESTEP_MINUTES)
    center = reference_time - lag_delta
    start = center - margin_delta
    end = min(center + margin_delta, reference_time)
    return start, end


def _rainfall_exceeded_in_window(
    rainfall_series: pd.DataFrame, reference_time: pd.Timestamp, lag_timesteps: int
) -> bool:
    """Whether rainfall_intensity exceeded RAINFALL_CONFIRM_MM_H at any point
    in the lag-centered lookback window (Layer 3's confirmation check)."""
    if rainfall_series.empty:
        return False
    start, end = _lag_window_bounds(reference_time, lag_timesteps, LAG_MARGIN)
    window = rainfall_series[
        (rainfall_series["timestamp"] >= start) & (rainfall_series["timestamp"] <= end)
    ]
    if window.empty:
        return False
    return bool((window["value"] >= RAINFALL_CONFIRM_MM_H).any())


def _rising_fast(water_level_series: pd.DataFrame, reference_time: pd.Timestamp) -> bool:
    """Layer 2: sustained rate of rise over the last 3 timesteps (15 min)."""
    window_minutes = RATE_OF_RISE_WINDOW_TIMESTEPS * TIMESTEP_MINUTES
    past_time = reference_time - pd.Timedelta(minutes=window_minutes)

    current_row = water_level_series[water_level_series["timestamp"] == reference_time]
    past_row = water_level_series[water_level_series["timestamp"] == past_time]
    if current_row.empty or past_row.empty:
        return False  # not enough history yet (e.g. near the start of the replay)

    rise_cm = current_row["value"].iloc[0] - past_row["value"].iloc[0]
    rate_cm_per_min = rise_cm / window_minutes
    return rate_cm_per_min >= RATE_OF_RISE_CM_PER_MIN


def _assess_sensor(readings_up_to_now: pd.DataFrame, sensor_id: str) -> SensorAssessment | None:
    """Layers 1-4 for one channel sensor. Returns None if there's no
    water_level data for it yet (e.g. before the replay reaches its first
    reading)."""
    water_level = get_series(readings_up_to_now, sensor_id, "water_level")
    if water_level.empty:
        return None

    latest_ts = water_level["timestamp"].iloc[-1]
    latest_wl = water_level["value"].iloc[-1]

    threshold_state = stage_for_water_level(latest_wl)   # Layer 1
    rising_fast = _rising_fast(water_level, latest_ts)   # Layer 2

    if sensor_id == UPSTREAM_SENSOR_ID:
        # S01 has no upstream neighbour to check. Confirm it instead via its
        # own rainfall (lag=0, so the window collapses to "the last
        # LAG_MARGIN timesteps") OR its own rate-of-rise — either is
        # independent physical evidence the reading is a real event rather
        # than a sensor fault, since there's nothing further upstream to
        # corroborate against.
        own_rainfall = get_series(readings_up_to_now, sensor_id, "rainfall_intensity")
        own_rain_confirmed = _rainfall_exceeded_in_window(own_rainfall, latest_ts, lag_timesteps=0)
        upstream_rain_confirmed = own_rain_confirmed or rising_fast
    else:
        lag = CUMULATIVE_LAG_FROM_S01[sensor_id]         # Layer 3
        upstream_rainfall = get_series(readings_up_to_now, UPSTREAM_SENSOR_ID, "rainfall_intensity")
        upstream_rain_confirmed = _rainfall_exceeded_in_window(upstream_rainfall, latest_ts, lag)

    high_water = threshold_state != "Normal"
    confirmed_flood = high_water and upstream_rain_confirmed        # Layer 3
    possible_fault = high_water and not upstream_rain_confirmed     # Layer 4: negation of Layer 3
    effective_state = threshold_state if confirmed_flood else "Normal"  # feeds Layer 5

    conditions = {
        "water_level_watch_plus": high_water,
        "rising_fast": rising_fast,
        "upstream_rain_confirmed": upstream_rain_confirmed,
    }

    return SensorAssessment(
        sensor_id=sensor_id,
        latest_timestamp=latest_ts,
        latest_water_level=latest_wl,
        threshold_state=threshold_state,
        rising_fast=rising_fast,
        upstream_rain_confirmed=upstream_rain_confirmed,
        confirmed_flood=confirmed_flood,
        possible_fault=possible_fault,
        effective_state=effective_state,
        conditions=conditions,
    )


def assess_risk(readings_up_to_now: pd.DataFrame, sensors_meta: list[dict]) -> RiskAssessment:
    """Full risk assessment from the system state at the current moment.

    readings_up_to_now: long-format DataFrame (sensor_id, timestamp,
    variable, value), already filtered to the replay's current moment.
    sensors_meta: sensor metadata list (e.g. from data_loader.load_sensors),
    used only to enumerate which channel sensors to assess.
    """
    sensor_assessments: dict[str, SensorAssessment] = {}
    for sensor in sensors_meta:
        sensor_id = sensor["sensor_id"]
        assessment = _assess_sensor(readings_up_to_now, sensor_id)
        if assessment is not None:
            sensor_assessments[sensor_id] = assessment

    # Layer 5: overall state is the worst *confirmed* severity across
    # sensors. Unconfirmed high readings (possible_fault) surface only as a
    # data-quality flag on their own sensor and do not escalate the
    # headline state.
    overall_state = "Normal"
    for assessment in sensor_assessments.values():
        if STAGES.index(assessment.effective_state) > STAGES.index(overall_state):
            overall_state = assessment.effective_state

    return RiskAssessment(overall_state=overall_state, sensors=sensor_assessments)
