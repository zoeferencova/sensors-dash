"""Load and reshape the synthetic sensor data (long-format JSON -> tidy DataFrame)."""

import json
from pathlib import Path

import pandas as pd

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sensor_data.json"
SENSORS_PATH = Path(__file__).resolve().parent.parent / "data" / "sensors.json"

# Variables that only ever appear on the four channel sensors (S01-S04), never
# on a catchment-wide record. Used to find real sensors regardless of whether
# soil_moisture is currently per-sensor or consolidated into a single CATCHMENT id.
CHANNEL_VARIABLES = ("water_level", "rainfall_intensity")


def load_long(path: Path = DATA_PATH) -> pd.DataFrame:
    """Load the raw long-format records into a DataFrame with parsed timestamps."""
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    df = pd.DataFrame.from_records(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def clean_readings(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee no NA/invalid values reach the alert logic or get_series.

    This is a data-integrity pass, not the alert logic's own fault detection
    (CLAUDE.md: that's about physically-implausible-but-present readings,
    not missing ones) — it just makes the "clean input" assumption those
    downstream layers rely on actually true:
    - non-numeric `value`s are coerced to NaN, then dropped along with any
      row missing a sensor_id/timestamp/variable
    - duplicate (sensor_id, timestamp, variable) rows collapse to the first
    - rows are sorted by timestamp so window/lookback logic sees them in order
    """
    cleaned = df.copy()
    cleaned["value"] = pd.to_numeric(cleaned["value"], errors="coerce")
    cleaned = cleaned.dropna(subset=["sensor_id", "timestamp", "variable", "value"])
    cleaned = cleaned.drop_duplicates(subset=["sensor_id", "timestamp", "variable"], keep="first")
    return cleaned.sort_values("timestamp").reset_index(drop=True)


def sensor_ids(df: pd.DataFrame) -> list[str]:
    """Channel sensor IDs (e.g. S01-S04), excluding any catchment-wide id."""
    mask = df["variable"].isin(CHANNEL_VARIABLES)
    return sorted(df.loc[mask, "sensor_id"].unique())


def get_series(df: pd.DataFrame, sensor_id: str, variable: str) -> pd.DataFrame:
    """One sensor's one variable over time, sorted, as columns [timestamp,
    value] — plus 'injected' too, if the event injector has added that flag
    column to `df` (a plain replay frame without it works exactly as before)."""
    mask = (df["sensor_id"] == sensor_id) & (df["variable"] == variable)
    cols = ["timestamp", "value"] + (["injected"] if "injected" in df.columns else [])
    return (
        df.loc[mask, cols]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def load_sensors(path: Path = SENSORS_PATH) -> list[dict]:
    """Load sensor metadata (id, name, lat/lon, role) used for the map."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def latest_timestamp(df: pd.DataFrame):
    """Most recent timestamp across all records."""
    return df["timestamp"].max()


def latest_value(df: pd.DataFrame, sensor_id: str, variable: str):
    """Latest (timestamp, value) for one sensor+variable, or None if there's no data."""
    series = get_series(df, sensor_id, variable)
    if series.empty:
        return None
    last = series.iloc[-1]
    return last["timestamp"], last["value"]
