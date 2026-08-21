"""Sensor map construction (PROJECT_BRIEF.md tech stack: dash-leaflet).

`dl.Map` and `dl.TileLayer` are built ONCE here and never appear as a
callback Output — tiles, pan, and zoom must never reset. Only the
`dl.LayerGroup`'s `children` are ever swapped by a callback (Step 4: the
per-tick risk-fanout callback in main.py rebuilds the marker list via
`build_markers` and writes it to that LayerGroup's `children` — nothing
else about the map is ever touched).
"""

import dash_leaflet as dl

from botic_reach_coords import BOTIC_REACH
from constants import NEUTRAL_PIN_COLOR, SEVERITY_COLORS

MARKER_LAYER_ID = "sensor-markers"
MARKER_ID_TYPE = "sensor-marker"  # pattern-matching id type for click selection

FAULT_STROKE_COLOR = "#4d4d4d"
"""possible_fault gets a black, thicker, dashed ring around whatever color
its raw (unconfirmed) severity would otherwise be — same fill so you can
still see how high it's reading, but the ring marks it as unconfirmed
rather than a real, escalated flood signal."""

SELECTED_RING_COLOR = "#3d5a73"
SELECTED_RING_RADIUS = 16
"""The currently-selected sensor gets a solid accent halo drawn AROUND its
pin — a separate, larger, unfilled CircleMarker rather than a stroke on the
pin itself, so it can't be confused with the dashed possible_fault ring (a
sensor can be both selected and faulted at once, and both must stay
readable). The color is deliberately outside the SEVERITY_COLORS palette:
it says "this is what you're looking at", not "this is how bad it is". It
is the same steel-blue the dashboard uses for every other selected/active
control (.sensor-tab-selected, .btn-primary), kept dark enough to separate
clearly from the much lighter REACH_LINE_COLOR it sits on top of."""

REACH_LINE_COLOR = "#98a4ae"
"""The Botič reach itself. Deliberately a neutral grey-slate with no hue of
its own: it is the one thing on the map that never changes state, so it
must not compete with the severity-coloured pins sitting on it. An earlier
muted blue read as "another status colour" and made the pins hard to pick
out against it."""


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
        return {"color": FAULT_STROKE_COLOR, "fillColor": fill, "weight": 2, "dashArray": "4"}

    fill = SEVERITY_COLORS.get(sensor_assessment.effective_state, NEUTRAL_PIN_COLOR)
    return {"color": fill, "fillColor": fill, "weight": 2, "dashArray": None}


def _status_label(sensor_assessment) -> str:
    if sensor_assessment.confirmed_flood:
        return "CONFIRMED FLOOD"
    if sensor_assessment.possible_fault:
        return "POSSIBLE FAULT"
    return sensor_assessment.threshold_state


def _selection_ring(sensor: dict) -> dl.CircleMarker:
    """The halo around the selected sensor's pin.

    `interactive=False` so it never swallows the click meant for the pin
    underneath it (it is drawn first, hence below, but it is also wider —
    without this its edge would be a dead zone around the marker). It
    carries no `id`: nothing addresses it as a callback target, and giving
    it a pattern-matching id would make it show up as a phantom entry in
    the marker-click ALL-input.
    """
    return dl.CircleMarker(
        center=[sensor["lat"], sensor["lon"]],
        radius=SELECTED_RING_RADIUS,
        color=SELECTED_RING_COLOR,
        weight=3,
        opacity=0.9,
        fill=False,
        dashArray=None,
        interactive=False,
    )


def build_markers(
    sensors_meta: list[dict], assessments: dict | None = None, selected_sensor: str | None = None
) -> list:
    """One CircleMarker per sensor, plus a selection halo for
    `selected_sensor` if given. `assessments` is `RiskAssessment.sensors`
    (sensor_id -> SensorAssessment) — omit it (or pass None) for the
    uniform-neutral pre-assessment render used at layout build time.

    The halo is emitted BEFORE its pin so Leaflet's SVG renderer paints it
    underneath (insertion order = paint order), leaving the pin's own fill
    and fault ring fully visible on top.
    """
    assessments = assessments or {}
    markers = []
    for sensor in sensors_meta:
        sensor_id = sensor["sensor_id"]
        sa = assessments.get(sensor_id)
        style = _marker_style(sa)

        tooltip_text = f"{sensor_id} · {sensor['name']}"
        if sa is not None:
            tooltip_text += f" · {_status_label(sa)}"

        if sensor_id == selected_sensor:
            markers.append(_selection_ring(sensor))

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


RIGHT_PANEL_WIDTH_PX = 300
"""Mirrors .right-panel's width in assets/style.css. That panel floats OVER
the map, so the map is wider than its visible area and a plain bounds-fit
would centre the reach under the panel — putting the easternmost sensor
(S01) behind the injector card. Reserved as fitBounds padding below."""


def build_map(sensors_meta: list[dict]) -> dl.Map:
    """Centered on the reach via bounds-fit rather than a fixed zoom: the
    four sensors span the reach mostly east-west, while the map panel is
    portrait, so a fixed center+zoom leaves some pins outside the viewport.
    Fitting to padded bounds guarantees all four are visible regardless of
    the panel's aspect ratio.

    `boundsOptions.paddingBottomRight` keeps the fit clear of the floating
    right panel. Doing it in pixels rather than by inflating the degree
    padding below is what makes it exact — the reserved strip is the panel's
    real width, not a guess that drifts with zoom or aspect ratio.
    """
    lats = [s["lat"] for s in sensors_meta]
    lons = [s["lon"] for s in sensors_meta]
    padding = 0.01  # degrees, keeps pins off the very edge
    bounds = [
        [min(lats) - padding, min(lons) - padding],
        [max(lats) + padding, max(lons) + padding],
    ]

    return dl.Map(
        bounds=bounds,
        boundsOptions={"paddingBottomRight": [RIGHT_PANEL_WIDTH_PX, 0]},
        style={"height": "100%", "width": "100%"},
        children=[
            dl.TileLayer(
                url="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
            ),
            dl.Polyline(
                positions=BOTIC_REACH,
                color=REACH_LINE_COLOR,
                weight=4,
                opacity=0.85,
            ),
            dl.LayerGroup(id=MARKER_LAYER_ID, children=build_markers(sensors_meta)),
        ],
    )
