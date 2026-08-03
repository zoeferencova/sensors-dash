"""Dash entry point — Step 3 of the Streamlit->Dash port (CLAUDE.md
"Dashboard sections (target layout)"). Adds the map, the sensor charts, and
the structural layout (top bar / left panel / center map / right panel).
Styling is intentionally bare — borders/backgrounds only where CLAUDE.md
calls for visual separation (left panel split, injector placeholder).

The two flicker-sensitive pieces follow CLAUDE.md's tech-stack notes
exactly:
- The map: dl.Map/dl.TileLayer are built once in sensor_map.build_map and
  never appear as callback Outputs; only the marker LayerGroup could be
  swapped later (not needed this step — pin color is uniform).
- The charts: dcc.Graph's `figure` is only ever reassigned when the
  selected sensor changes (an explicit, infrequent user action). Every
  replay tick instead writes `extendData`, which tells the client to
  Plotly.extendTraces the new point(s) onto the existing figure in place —
  no re-render, no teardown.
"""

from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

import charts
from data_loader import clean_readings, get_series, load_long, load_sensors
from event_injector import apply_injections
from replay import build_timeline, visible_readings
from sensor_map import MARKER_ID_TYPE, build_map

READINGS = clean_readings(load_long())
SENSORS_META = load_sensors()
TIMELINE = build_timeline(READINGS)
SENSOR_IDS = [s["sensor_id"] for s in SENSORS_META]
DEFAULT_SENSOR = SENSOR_IDS[0]

SPEED_OPTIONS = [
    {"label": "1x", "value": 1000},
    {"label": "2x", "value": 500},
    {"label": "4x", "value": 250},
    {"label": "8x", "value": 125},
]

app = Dash(__name__)

# --- Top bar ----------------------------------------------------------

top_bar = html.Div(
    id="top-bar",
    style={
        "display": "flex",
        "alignItems": "center",
        "gap": "24px",
        "padding": "8px 16px",
        "borderBottom": "1px solid #ccc",
        "flexWrap": "wrap",
    },
    children=[
        html.Div(id="overall-risk-placeholder", children="Overall risk: (placeholder)"),
        html.Div(id="clock-display"),
        html.Button("Play", id="play-pause-btn", n_clicks=0),
        dcc.RadioItems(id="speed-control", options=SPEED_OPTIONS, value=1000, inline=True),
        # Room for injector-related global bits (e.g. "N active events") once the injector exists.
        html.Div(id="top-bar-injector-slot"),
    ],
)

# --- Left panel ---------------------------------------------------------

left_panel_top = html.Div(
    id="left-panel-top",
    style={"flex": "1 1 auto", "overflowY": "auto", "padding": "8px"},
    children=[
        dcc.Dropdown(
            id="sensor-selector",
            options=[{"label": f"{s['sensor_id']} — {s['name']}", "value": s["sensor_id"]} for s in SENSORS_META],
            value=DEFAULT_SENSOR,
            clearable=False,
        ),
        dcc.Graph(id="water-level-graph", figure={}),
        dcc.Graph(id="rainfall-graph", figure={}),
        html.Div(id="current-readings-placeholder", children="Current readings: (placeholder)"),
    ],
)

left_panel_bottom = html.Div(
    id="left-panel-bottom",
    style={"borderTop": "2px solid #999", "padding": "8px", "flex": "0 0 auto"},
    children=[
        html.H4("All-sensor status"),
        html.Div("(placeholder)"),
    ],
)

left_panel = html.Div(
    id="left-panel",
    style={
        "width": "360px",
        "flex": "0 0 360px",
        "display": "flex",
        "flexDirection": "column",
        "borderRight": "1px solid #ccc",
        "minHeight": 0,
    },
    children=[left_panel_top, left_panel_bottom],
)

# --- Center: map ----------------------------------------------------------

map_panel = html.Div(id="map-panel", style={"flex": "1 1 auto", "minWidth": 0}, children=[build_map(SENSORS_META)])

# --- Right panel ----------------------------------------------------------

injector_placeholder = html.Div(
    id="injector-placeholder",
    style={
        "border": "2px dashed #b35900",
        "background": "#fff3e6",
        "padding": "8px",
        "margin": "8px",
    },
    children=[
        html.H4("Event injector"),
        html.Div("(placeholder — scenario controls come in a later step)"),
    ],
)

event_log_placeholder = html.Div(
    id="event-log-placeholder",
    style={"padding": "8px", "margin": "8px"},
    children=[
        html.H4("Event log"),
        html.Div("(placeholder)"),
    ],
)

right_panel = html.Div(
    id="right-panel",
    style={"width": "300px", "flex": "0 0 300px", "display": "flex", "flexDirection": "column", "minHeight": 0},
    children=[injector_placeholder, event_log_placeholder],
)

# --- Assemble ----------------------------------------------------------

body_row = html.Div(
    id="body-row",
    style={"display": "flex", "flex": "1 1 auto", "minHeight": 0},
    children=[left_panel, map_panel, right_panel],
)

app.layout = html.Div(
    style={"display": "flex", "flexDirection": "column", "height": "100vh"},
    children=[
        top_bar,
        body_row,
        # Advanced only by advance_replay — the single source of truth for
        # "what step is the replay on" that any callback reads instead of
        # reaching into the Interval component's own n_intervals counter.
        dcc.Store(id="sim-step-store", data=0),
        # Holds InjectedEvent instances once the injector UI appends to it.
        # Empty for now, but already wired: every tick feeds it through
        # apply_injections and writes back exactly the pruned list.
        dcc.Store(id="active-events-store", data=[]),
        # Which sensor's charts are shown in the left panel — set by the
        # dropdown or a map-pin click.
        dcc.Store(id="selected-sensor-store", data=DEFAULT_SENSOR),
        # Tracks what's already been drawn into the two Graphs, so the
        # chart callback knows whether to extendData (same sensor, one
        # tick further) or rebuild the figure (sensor just changed).
        dcc.Store(id="chart-state-store", data={"sensor_id": None, "rendered_upto_step": -1}),
        # disabled=True: replay opens paused at the first timestep, per
        # CLAUDE.md — history builds up as it plays, not fully populated.
        dcc.Interval(id="replay-interval", interval=1000, n_intervals=0, disabled=True),
    ],
)


# --- Callbacks ----------------------------------------------------------


@app.callback(
    Output("replay-interval", "disabled"),
    Output("play-pause-btn", "children"),
    Input("play-pause-btn", "n_clicks"),
    State("replay-interval", "disabled"),
    prevent_initial_call=True,
)
def toggle_play_pause(_n_clicks, is_disabled):
    now_disabled = not is_disabled
    return now_disabled, "Play" if now_disabled else "Pause"


@app.callback(
    Output("replay-interval", "interval"),
    Input("speed-control", "value"),
)
def set_speed(interval_ms):
    return interval_ms


@app.callback(
    Output("sim-step-store", "data"),
    Output("active-events-store", "data"),
    Output("clock-display", "children"),
    Input("replay-interval", "n_intervals"),
    State("active-events-store", "data"),
)
def advance_replay(n_intervals, active_events):
    """Runs once on page load (n_intervals=0, paused) and again on every
    Interval tick thereafter — it never fires while paused, since a
    disabled dcc.Interval simply doesn't tick. sim_step is a pure function
    of n_intervals, clamped to the last timestep so replay stops rather than
    erroring once the data runs out."""
    sim_step = min(n_intervals, len(TIMELINE) - 1)
    visible = visible_readings(READINGS, TIMELINE, sim_step)

    # active_events is always [] today (no injector UI yet), so this is a
    # no-op — but it's the exact call the injector's Trigger button will
    # feed into later: readings in, (injected readings, pruned still-active
    # list) out, written straight back to active-events-store.
    _injected, still_active = apply_injections(visible, TIMELINE, active_events, sim_step)

    current_time = TIMELINE[sim_step]
    display = html.Div(
        [
            html.Span(f"sim_step: {sim_step} / {len(TIMELINE) - 1}"),
            html.Span(f"  |  timestamp: {current_time}"),
        ]
    )
    return sim_step, still_active, display


@app.callback(
    Output("selected-sensor-store", "data"),
    Input("sensor-selector", "value"),
    Input({"type": MARKER_ID_TYPE, "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_sensor(dropdown_value, _marker_clicks):
    """Either the dropdown or a map-pin click can drive selection; whichever
    fired is identified via ctx.triggered_id (a plain string for the
    dropdown, a {"type", "index"} dict for a pattern-matched marker)."""
    triggered = ctx.triggered_id
    if isinstance(triggered, dict) and triggered.get("type") == MARKER_ID_TYPE:
        return triggered["index"]
    return dropdown_value


@app.callback(
    Output("water-level-graph", "figure"),
    Output("water-level-graph", "extendData"),
    Output("rainfall-graph", "figure"),
    Output("rainfall-graph", "extendData"),
    Output("chart-state-store", "data"),
    Input("sim-step-store", "data"),
    Input("selected-sensor-store", "data"),
    State("chart-state-store", "data"),
)
def update_charts(sim_step, selected_sensor, chart_state):
    """The extendData pattern (CLAUDE.md tech stack notes): on a sensor
    change, rebuild both figures from scratch with the full history visible
    so far (an explicit, infrequent user action — a full figure resend here
    is fine). On every other call (a replay tick with the same sensor
    selected), only the row(s) newer than what's already drawn are sent via
    extendData, so the figure itself is never reassigned during playback."""
    visible = visible_readings(READINGS, TIMELINE, sim_step)
    water_level_series = get_series(visible, selected_sensor, "water_level")
    rainfall_series = get_series(visible, selected_sensor, "rainfall_intensity")

    sensor_changed = chart_state.get("sensor_id") != selected_sensor
    new_state = {"sensor_id": selected_sensor, "rendered_upto_step": sim_step}

    if sensor_changed:
        water_level_fig = charts.build_water_level_figure(selected_sensor)
        charts.update_water_level_figure(water_level_fig, water_level_series)
        rainfall_fig = charts.build_rainfall_figure(selected_sensor)
        charts.update_rainfall_figure(rainfall_fig, rainfall_series)
        return water_level_fig, no_update, rainfall_fig, no_update, new_state

    last_rendered_step = chart_state.get("rendered_upto_step", -1)
    if sim_step <= last_rendered_step:
        return no_update, no_update, no_update, no_update, chart_state

    cutoff = TIMELINE[last_rendered_step]
    new_water_level = water_level_series[water_level_series["timestamp"] > cutoff]
    new_rainfall = rainfall_series[rainfall_series["timestamp"] > cutoff]

    water_level_extend = (
        {"x": [new_water_level["timestamp"].tolist()], "y": [new_water_level["value"].tolist()]},
        [0],
    )
    rainfall_extend = (
        {"x": [new_rainfall["timestamp"].tolist()], "y": [new_rainfall["value"].tolist()]},
        [0],
    )
    return no_update, water_level_extend, no_update, rainfall_extend, new_state


if __name__ == "__main__":
    # 8050 (Dash's default) collides with an unrelated project's leftover
    # server on this machine — a dedicated port avoids that fight entirely.
    app.run(debug=True, port=8060)
