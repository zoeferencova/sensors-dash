"""Sensor map construction (CLAUDE.md tech stack: dash-leaflet).

`dl.Map` and `dl.TileLayer` are built ONCE here and never appear as a
callback Output — tiles, pan, and zoom must never reset. Only the
`dl.LayerGroup`'s `children` are ever swapped by a callback (Step 4: the
per-tick risk-fanout callback in main.py rebuilds the marker list via
`build_markers` and writes it to that LayerGroup's `children` — nothing
else about the map is ever touched).
"""

import dash_leaflet as dl

from constants import NEUTRAL_PIN_COLOR, SEVERITY_COLORS

MARKER_LAYER_ID = "sensor-markers"
MARKER_ID_TYPE = "sensor-marker"  # pattern-matching id type for click selection

FAULT_STROKE_COLOR = "#000000"
"""possible_fault gets a black, thicker, dashed ring around whatever color
its raw (unconfirmed) severity would otherwise be — same fill so you can
still see how high it's reading, but the ring marks it as unconfirmed
rather than a real, escalated flood signal."""


def marker_id(sensor_id: str) -> dict:
    return {"type": MARKER_ID_TYPE, "index": sensor_id}


def _marker_style(sensor_assessment) -> dict:
    """Fill/stroke for one pin. No assessment yet (e.g. before the first
    tick's fanout runs) falls back to the neutral color from constants.py —
    the same "before assess_risk has produced a per-sensor state" case that
    constant's docstring already names."""
    if sensor_assessment is None:
        return {"color": NEUTRAL_PIN_COLOR, "fillColor": NEUTRAL_PIN_COLOR, "weight": 2, "dashArray": None}

    if sensor_assessment.possible_fault:
        fill = SEVERITY_COLORS.get(sensor_assessment.threshold_state, NEUTRAL_PIN_COLOR)
        return {"color": FAULT_STROKE_COLOR, "fillColor": fill, "weight": 3, "dashArray": "4"}

    fill = SEVERITY_COLORS.get(sensor_assessment.effective_state, NEUTRAL_PIN_COLOR)
    return {"color": fill, "fillColor": fill, "weight": 2, "dashArray": None}


def _status_label(sensor_assessment) -> str:
    if sensor_assessment.confirmed_flood:
        return "CONFIRMED FLOOD"
    if sensor_assessment.possible_fault:
        return "POSSIBLE FAULT"
    return sensor_assessment.threshold_state


def build_markers(sensors_meta: list[dict], assessments: dict | None = None) -> list[dl.CircleMarker]:
    """One CircleMarker per sensor. `assessments` is `RiskAssessment.sensors`
    (sensor_id -> SensorAssessment) — omit it (or pass None) for the
    uniform-neutral pre-assessment render used at layout build time."""
    assessments = assessments or {}
    markers = []
    for sensor in sensors_meta:
        sensor_id = sensor["sensor_id"]
        sa = assessments.get(sensor_id)
        style = _marker_style(sa)

        tooltip_text = f"{sensor_id} — {sensor['name']}"
        if sa is not None:
            tooltip_text += f" — {_status_label(sa)}"

        markers.append(
            dl.CircleMarker(
                id=marker_id(sensor_id),
                center=[sensor["lat"], sensor["lon"]],
                radius=10,
                fillOpacity=0.9,
                children=[dl.Tooltip(tooltip_text)],
                **style,
            )
        )
    return markers


def build_map(sensors_meta: list[dict]) -> dl.Map:
    """Centered on the reach via bounds-fit rather than a fixed zoom: the
    four sensors span the reach mostly east-west, while the map panel is
    portrait, so a fixed center+zoom leaves some pins outside the viewport.
    Fitting to padded bounds guarantees all four are visible regardless of
    the panel's aspect ratio."""
    lats = [s["lat"] for s in sensors_meta]
    lons = [s["lon"] for s in sensors_meta]
    padding = 0.01  # degrees, keeps pins off the very edge
    bounds = [
        [min(lats) - padding, min(lons) - padding],
        [max(lats) + padding, max(lons) + padding],
    ]

    return dl.Map(
        bounds=bounds,
        style={"height": "100%", "width": "100%"},
        children=[
            dl.TileLayer(),
            dl.LayerGroup(id=MARKER_LAYER_ID, children=build_markers(sensors_meta)),
        ],
    )
