"""Sensor map construction (CLAUDE.md tech stack: dash-leaflet).

`dl.Map` and `dl.TileLayer` are built ONCE here and never appear as a
callback Output — tiles, pan, and zoom must never reset. Only a
`dl.LayerGroup`'s `children` are ever swapped by a callback. This step
doesn't need that yet (pin color is uniform, so nothing changes it after
first render), but the LayerGroup's id is wired up now so a later step
(recoloring pins by risk state) only has to touch its `children`, never the
Map/TileLayer around it.
"""

import dash_leaflet as dl

from constants import NEUTRAL_PIN_COLOR

MARKER_LAYER_ID = "sensor-markers"
MARKER_ID_TYPE = "sensor-marker"  # pattern-matching id type for click selection


def marker_id(sensor_id: str) -> dict:
    return {"type": MARKER_ID_TYPE, "index": sensor_id}


def build_markers(sensors_meta: list[dict]) -> list[dl.CircleMarker]:
    """One CircleMarker per sensor, uniform color for now (per this step's
    scope). Each carries a pattern-matching id so a single callback can
    identify which pin was clicked via an ALL-matching Input on n_clicks."""
    return [
        dl.CircleMarker(
            id=marker_id(sensor["sensor_id"]),
            center=[sensor["lat"], sensor["lon"]],
            radius=10,
            color=NEUTRAL_PIN_COLOR,
            fillColor=NEUTRAL_PIN_COLOR,
            fillOpacity=0.9,
            children=[dl.Tooltip(f"{sensor['sensor_id']} — {sensor['name']}")],
        )
        for sensor in sensors_meta
    ]


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
