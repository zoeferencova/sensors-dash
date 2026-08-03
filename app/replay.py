"""The replay engine (CLAUDE.md "The replay engine"): static synthetic data
stepped through on a timer to read as "live". Owns the timeline — the
ordered sequence of timesteps the whole app plays through — and the single
function that turns "current step index" into the readings frame everything
else reads. Charts, the map, and risk_assessment will all consume
`visible_readings` once wired in; nothing downstream should re-filter the
full dataset by timestamp itself.
"""

import pandas as pd


def build_timeline(df: pd.DataFrame) -> list[pd.Timestamp]:
    """Ordered, de-duplicated timestamps across the whole dataset. sim_step
    is an index into this list — it's the replay clock's tick sequence."""
    return sorted(df["timestamp"].unique())


def visible_readings(df: pd.DataFrame, timeline: list[pd.Timestamp], sim_step: int) -> pd.DataFrame:
    """Readings visible at `sim_step`: every row up to and including
    timeline[sim_step]. History builds up as the replay advances rather
    than the dashboard opening fully populated (CLAUDE.md default state)."""
    sim_step = max(0, min(sim_step, len(timeline) - 1))
    current_time = timeline[sim_step]
    return df[df["timestamp"] <= current_time]
