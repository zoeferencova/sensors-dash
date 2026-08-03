"""Dash entry point — Steps 3-4 of the Streamlit->Dash port.

Step 3 (CLAUDE.md "Dashboard sections (target layout)") built the map, the
sensor charts, and the structural layout. Step 4 (CLAUDE.md "The alert /
nowcasting logic") wires the already-ported `risk_assessment.assess_risk`
into that layout: one callback (`update_risk_fanout`) runs it once per tick
and fans the single result out to the map pins, the top-bar overall state,
the current-readings and rule-evaluation panels, the all-sensor summary,
and the event log. Nothing downstream recomputes risk itself — they only
read fields off the `RiskAssessment`/`SensorAssessment` that callback
produces.

The two flicker-sensitive pieces from Step 3 still hold:
- The map: dl.Map/dl.TileLayer are built once in sensor_map.build_map and
  never appear as callback Outputs. Only the marker LayerGroup's `children`
  are swapped (now every tick, to recolor pins by status) — Map/TileLayer
  themselves are never touched, so panning/zoom/tiles never reset.
- The charts: dcc.Graph's `figure` is only ever reassigned when the
  selected sensor changes (an explicit, infrequent user action). Every
  replay tick instead writes `extendData`, which tells the client to
  Plotly.extendTraces the new point(s) onto the existing figure in place —
  no re-render, no teardown.
"""

import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

import charts
from constants import SEVERITY_COLORS
from data_loader import clean_readings, get_series, latest_value, load_long, load_sensors
from event_injector import (
    DEFAULT_MAGNITUDE,
    SCENARIO_DESCRIPTIONS,
    SCENARIOS,
    SCENARIOS_NEEDING_TARGET,
    UPSTREAM_SENSOR_ID,
    apply_injections,
    build_event,
    events_from_store,
    events_signature,
    events_to_store,
)
from replay import build_timeline, visible_readings
from risk_assessment import RAINFALL_CONFIRM_MM_H, assess_risk
from sensor_map import MARKER_ID_TYPE, MARKER_LAYER_ID, build_map, build_markers

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
        html.Div(id="overall-risk-display"),
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
        # A real (if empty) Figure rather than {} — Plotly.js otherwise logs
        # a harmless but noisy "doesn't yet have a plot" warning on first
        # paint, before update_charts's initial call replaces it moments
        # later anyway.
        dcc.Graph(id="water-level-graph", figure=go.Figure()),
        dcc.Graph(id="rainfall-graph", figure=go.Figure()),
        html.Div(id="current-readings-panel"),
        html.Div(id="rule-eval-panel"),
    ],
)

left_panel_bottom = html.Div(
    id="left-panel-bottom",
    style={"borderTop": "2px solid #999", "padding": "8px", "flex": "0 0 auto"},
    children=[
        html.H4("All-sensor status"),
        html.Div(id="all-sensor-status-panel"),
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

DEFAULT_TARGET_SENSOR = UPSTREAM_SENSOR_ID if UPSTREAM_SENSOR_ID in SENSOR_IDS else DEFAULT_SENSOR

injector_panel = html.Div(
    id="injector-placeholder",
    style={
        "border": "2px dashed #b35900",
        "background": "#fff3e6",
        "padding": "8px",
        "margin": "8px",
    },
    children=[
        html.H4("Event injector"),
        html.Div(
            "Overlays synthetic readings onto the stream assess_risk sees. It "
            "never sets the risk state directly, and never touches the "
            "underlying dataset — Reset always returns to a clean replay.",
            style={"fontSize": "0.8em", "marginBottom": "8px"},
        ),
        html.Label("Scenario"),
        dcc.Dropdown(
            id="injector-scenario-select",
            options=[{"label": s, "value": s} for s in SCENARIOS],
            value=SCENARIOS[0],
            clearable=False,
        ),
        html.Div(id="injector-scenario-description", style={"fontSize": "0.8em", "margin": "6px 0"}),
        html.Div(
            id="injector-target-sensor-row",
            children=[
                html.Label("Target sensor"),
                dcc.Dropdown(
                    id="injector-target-sensor-select",
                    options=[{"label": sid, "value": sid} for sid in SENSOR_IDS],
                    value=DEFAULT_TARGET_SENSOR,
                    clearable=False,
                ),
            ],
        ),
        html.Div(
            style={"display": "flex", "gap": "8px", "marginTop": "8px"},
            children=[
                html.Button("Trigger scenario", id="injector-trigger-btn", n_clicks=0),
                html.Button("Reset injected events", id="injector-reset-btn", n_clicks=0),
            ],
        ),
        html.Div(id="injector-active-events-display", style={"marginTop": "8px"}),
    ],
)

event_log_placeholder = html.Div(
    id="event-log-placeholder",
    style={"padding": "8px", "margin": "8px"},
    children=[
        html.H4("Event log"),
        html.Div(id="event-log-content"),
    ],
)

right_panel = html.Div(
    id="right-panel",
    style={"width": "300px", "flex": "0 0 300px", "display": "flex", "flexDirection": "column", "minHeight": 0},
    children=[injector_panel, event_log_placeholder],
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
        # Append-only log entries (strings), newest kept up to MAX_LOG_ENTRIES.
        dcc.Store(id="event-log-store", data=[]),
        # Previous tick's per-sensor category (confirmed_flood/possible_fault/
        # normal) — used only to detect transitions for the event log; never
        # displayed directly. Empty dict means "no history yet" (page load).
        dcc.Store(id="sensor-status-store", data={}),
        # disabled=True: replay opens paused at the first timestep, per
        # CLAUDE.md — history builds up as it plays, not fully populated.
        dcc.Interval(id="replay-interval", interval=1000, n_intervals=0, disabled=True),
    ],
)


# --- Callbacks ----------------------------------------------------------


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
    Output("replay-interval", "disabled"),
    Output("play-pause-btn", "children"),
    Input("replay-interval", "n_intervals"),
    Input("play-pause-btn", "n_clicks"),
    Input("injector-trigger-btn", "n_clicks"),
    Input("injector-reset-btn", "n_clicks"),
    State("active-events-store", "data"),
    State("injector-scenario-select", "value"),
    State("injector-target-sensor-select", "value"),
    State("replay-interval", "disabled"),
)
def advance_replay(
    n_intervals, _play_clicks, _trigger_clicks, _reset_clicks, active_events_raw, scenario, target_sensor, is_disabled
):
    """Owns active-events-store AND replay-interval/play-pause-btn, since
    Dash requires exactly one callback per Output and both are now touched
    from more than one control (the tick itself, Play/Pause, Trigger,
    Reset) — branches on ctx.triggered_id. sim_step is a pure function of
    n_intervals (clamped to the last timestep) and is recomputed the same
    way regardless of which Input fired, since Trigger needs "now" as the
    new event's trigger_step.
    """
    sim_step = min(n_intervals, len(TIMELINE) - 1)
    triggered = ctx.triggered_id

    if triggered == "play-pause-btn":
        now_disabled = not is_disabled
        return no_update, no_update, no_update, now_disabled, ("Play" if now_disabled else "Pause")

    if triggered == "injector-reset-btn":
        # active_events_raw is already the raw (dict) list — no need to
        # deserialize just to check emptiness. Skips a spurious Store write
        # (and the callbacks it would re-trigger) when there was nothing to
        # clear.
        events_output = no_update if not active_events_raw else []
        return no_update, events_output, no_update, no_update, no_update

    active_events = events_from_store(active_events_raw)

    if triggered == "injector-trigger-btn":
        new_event = build_event(scenario, target_sensor, DEFAULT_MAGNITUDE[scenario], sim_step)
        return no_update, events_to_store(active_events + [new_event]), no_update, no_update, no_update

    # Default path: a replay-interval tick (or the initial page-load call).
    # Never fires while paused, since a disabled dcc.Interval simply doesn't
    # tick.
    visible = visible_readings(READINGS, TIMELINE, sim_step)
    _injected, still_active = apply_injections(visible, TIMELINE, active_events, sim_step)

    current_time = TIMELINE[sim_step]
    display = html.Div(
        [
            html.Span(f"sim_step: {sim_step} / {len(TIMELINE) - 1}"),
            html.Span(f"  |  timestamp: {current_time}"),
        ]
    )

    # Only write active-events-store when an event actually expired this
    # tick — writing a freshly-reserialized-but-value-identical list every
    # tick regardless would spuriously re-trigger every callback that Inputs
    # on this Store (update_charts, update_risk_fanout) on EVERY tick, not
    # just when something real changed. That doubled invocation rate is
    # exactly what widens the window for response-ordering races (e.g. a
    # sensor switch landing right on a tick can otherwise show the
    # previous sensor's chart title).
    events_unchanged = events_signature(still_active) == events_signature(active_events)
    events_output = no_update if events_unchanged else events_to_store(still_active)

    # Auto-pause once the replay reaches the last timestep — otherwise the
    # Interval keeps firing forever with nothing left to advance to, and
    # "Pause" stays displayed even though playback has effectively stopped.
    at_end = sim_step >= len(TIMELINE) - 1
    disabled_output = True if at_end else no_update
    button_output = "Play" if at_end else no_update

    return sim_step, events_output, display, disabled_output, button_output


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
    Output("injector-scenario-description", "children"),
    Output("injector-target-sensor-row", "style"),
    Input("injector-scenario-select", "value"),
)
def update_injector_scenario_controls(scenario):
    """Scenario picker drives the description text and whether the
    target-sensor control is shown at all — "Catchment-wide event" hits
    every sensor per CLAUDE.md, so it has no single target
    (SCENARIOS_NEEDING_TARGET is the same set build_event's scenarios use,
    not re-derived here). Magnitude is a fixed per-scenario preset
    (DEFAULT_MAGNITUDE, used directly by the Trigger callback) rather than
    a control — one less thing to configure or explain."""
    description = SCENARIO_DESCRIPTIONS[scenario]
    target_row_style = {} if scenario in SCENARIOS_NEEDING_TARGET else {"display": "none"}
    return description, target_row_style


@app.callback(
    Output("water-level-graph", "figure"),
    Output("water-level-graph", "extendData"),
    Output("rainfall-graph", "figure"),
    Output("rainfall-graph", "extendData"),
    Output("chart-state-store", "data"),
    Input("sim-step-store", "data"),
    Input("selected-sensor-store", "data"),
    Input("active-events-store", "data"),
    State("chart-state-store", "data"),
)
def update_charts(sim_step, selected_sensor, active_events_raw, chart_state):
    """The extendData pattern (CLAUDE.md tech stack notes): on a sensor
    change OR the active-events set actually changing, rebuild both figures
    from scratch with the full history visible so far (both are explicit,
    infrequent actions — a full figure resend is fine). On every other call
    (a replay tick, same sensor, same events), only the row(s) newer than
    what's already drawn are sent via extendData.

    A just-triggered event needs the full-rebuild path even though it
    doesn't change sim_step: build_event's trigger_step is "now", and
    _pulse returns a nonzero factor at relative_step=0 — so the CURRENTLY
    visible point's value changes retroactively the instant an event is
    triggered (or reverts the instant one expires/is reset). A signature
    over the active set (not just its length) catches both directions.
    """
    active_events = events_from_store(active_events_raw)
    visible = visible_readings(READINGS, TIMELINE, sim_step)
    injected, _ = apply_injections(visible, TIMELINE, active_events, sim_step)
    water_level_series = get_series(injected, selected_sensor, "water_level")
    rainfall_series = get_series(injected, selected_sensor, "rainfall_intensity")

    current_signature = events_signature(active_events)
    sensor_changed = chart_state.get("sensor_id") != selected_sensor
    events_changed = chart_state.get("events_signature") != current_signature
    new_state = {
        "sensor_id": selected_sensor,
        "rendered_upto_step": sim_step,
        "events_signature": current_signature,
    }

    if sensor_changed or events_changed:
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

    # Trace 0 (main line/bar) always gets the new point(s); trace 1 (the
    # "simulated event" diamond overlay) gets only whichever of those are
    # flagged injected — possibly none, a harmless empty extend for that
    # trace. Without this, only the very first point of a multi-tick
    # injected event (sent by the full-rebuild that fires on trigger) would
    # ever show the marker; later ticks during the same event would raise
    # the line correctly but silently drop the "this is simulated" flag.
    new_water_level_injected = charts.injected_points(new_water_level)
    new_rainfall_injected = charts.injected_points(new_rainfall)

    water_level_extend = (
        {
            "x": [new_water_level["timestamp"].tolist(), new_water_level_injected["timestamp"].tolist()],
            "y": [new_water_level["value"].tolist(), new_water_level_injected["value"].tolist()],
        },
        [0, 1],
    )
    rainfall_extend = (
        {
            "x": [new_rainfall["timestamp"].tolist(), new_rainfall_injected["timestamp"].tolist()],
            "y": [new_rainfall["value"].tolist(), new_rainfall_injected["value"].tolist()],
        },
        [0, 1],
    )
    return no_update, water_level_extend, no_update, rainfall_extend, new_state


# --- Risk-assessment fanout (Step 4) --------------------------------------

MAX_LOG_ENTRIES = 50


def _verdict_label(sensor_assessment) -> str:
    if sensor_assessment.confirmed_flood:
        return "CONFIRMED FLOOD"
    if sensor_assessment.possible_fault:
        return "POSSIBLE FAULT"
    return sensor_assessment.threshold_state


def _latest_soil_moisture(df, sensor_id):
    """Defensive per CLAUDE.md's data contract note: soil_moisture may still
    be per-sensor (old data) or consolidated onto a single CATCHMENT id
    (new). Try the sensor itself first, then fall back to CATCHMENT.
    Returns ((timestamp, value) | None, used_catchment: bool)."""
    result = latest_value(df, sensor_id, "soil_moisture")
    if result is not None:
        return result, False
    catchment_result = latest_value(df, "CATCHMENT", "soil_moisture")
    # used_catchment must reflect whether the fallback actually found
    # something — not just that it was attempted. Otherwise "no soil_moisture
    # anywhere at all" (result is None) reports used_catchment=True, which is
    # wrong (there's no CATCHMENT data to have used) even though it's
    # currently harmless: the caller only checks this flag when a value
    # exists.
    return catchment_result, catchment_result is not None


def _render_overall_risk(overall_state: str) -> html.Span:
    color = SEVERITY_COLORS[overall_state]
    text_color = "#fff" if overall_state in ("Alert", "Danger", "Extreme") else "#000"
    return html.Span(
        f"Overall risk: {overall_state}",
        style={
            "backgroundColor": color,
            "color": text_color,
            "padding": "4px 10px",
            "borderRadius": "4px",
            "fontWeight": "bold",
        },
    )


def _render_current_readings(sensor_assessment, rainfall_latest, soil_latest, soil_is_catchment) -> html.Div:
    if sensor_assessment is None:
        return html.Div([html.H4("Current readings"), html.Div("No data yet.")])

    rows = [html.H4("Current readings")]
    stage_color = SEVERITY_COLORS[sensor_assessment.threshold_state]
    rows.append(
        html.Div(
            f"water_level: {sensor_assessment.latest_water_level:.1f} cm — {sensor_assessment.threshold_state}",
            style={"color": stage_color, "fontWeight": "bold"},
        )
    )

    if rainfall_latest is not None:
        _, rain_value = rainfall_latest
        flag = " (meaningful)" if rain_value >= RAINFALL_CONFIRM_MM_H else ""
        rows.append(html.Div(f"rainfall_intensity: {rain_value:.1f} mm/h{flag}"))
    else:
        rows.append(html.Div("rainfall_intensity: no data"))

    if soil_latest is not None:
        _, soil_value = soil_latest
        source = " (CATCHMENT)" if soil_is_catchment else ""
        rows.append(html.Div(f"soil_moisture: {soil_value:.1f}%{source}"))
    else:
        rows.append(html.Div("soil_moisture: no data"))

    return html.Div(rows)


def _render_rule_eval(sensor_assessment) -> html.Div:
    if sensor_assessment is None:
        return html.Div([html.H4("Rule evaluation"), html.Div("No data yet.")])

    conditions = sensor_assessment.conditions

    def mark(flag: bool) -> str:
        return "✓" if flag else "✗"

    return html.Div(
        [
            html.H4("Rule evaluation"),
            html.Div(f"water_level ≥ Watch: {mark(conditions['water_level_watch_plus'])}"),
            html.Div(f"rising_fast: {mark(conditions['rising_fast'])}"),
            html.Div(f"upstream_rain_confirmed: {mark(conditions['upstream_rain_confirmed'])}"),
            html.Div(html.B(f"→ {_verdict_label(sensor_assessment)}")),
        ]
    )


def _render_all_sensor_status(assessment) -> html.Div:
    rows = []
    for sensor_id in SENSOR_IDS:
        sensor_assessment = assessment.sensors.get(sensor_id)
        if sensor_assessment is None:
            rows.append(html.Div(f"{sensor_id}: no data"))
            continue
        color = SEVERITY_COLORS[
            sensor_assessment.threshold_state if sensor_assessment.possible_fault else sensor_assessment.effective_state
        ]
        rows.append(
            html.Div(
                [
                    html.Span("●", style={"color": color, "marginRight": "6px"}),
                    f"{sensor_id}: {_verdict_label(sensor_assessment)} ({sensor_assessment.latest_water_level:.0f} cm)",
                ]
            )
        )
    return html.Div(rows)


def _sensor_category(sensor_assessment) -> str:
    if sensor_assessment.confirmed_flood:
        return "confirmed_flood"
    if sensor_assessment.possible_fault:
        return "possible_fault"
    return "normal"


_CATEGORY_LABELS = {"confirmed_flood": "CONFIRMED FLOOD", "possible_fault": "POSSIBLE FAULT", "normal": "Normal"}


def _update_event_log(assessment, previous_categories: dict, log_entries: list) -> tuple[dict, list]:
    """Edge-triggered per CLAUDE.md: append a log line only when a sensor's
    category (confirmed_flood/possible_fault/normal) actually changes from
    the previous tick, never on every tick. An empty `previous_categories`
    means no history yet (page load) — seed it silently rather than logging
    every sensor's initial state as a fake "transition"."""
    current_categories = {sid: _sensor_category(sa) for sid, sa in assessment.sensors.items()}
    is_first_run = not previous_categories
    new_entries = list(log_entries)

    if not is_first_run:
        for sensor_id, category in current_categories.items():
            previous = previous_categories.get(sensor_id)
            if previous is not None and previous != category:
                sa = assessment.sensors[sensor_id]
                new_entries.append(
                    f"{sa.latest_timestamp} — {sensor_id}: {_CATEGORY_LABELS[previous]} -> "
                    f"{_CATEGORY_LABELS[category]} (water_level={sa.latest_water_level:.1f} cm, {sa.threshold_state})"
                )

    return current_categories, new_entries[-MAX_LOG_ENTRIES:]


def _render_event_log(log_entries: list) -> html.Div:
    if not log_entries:
        return html.Div("No events yet.")
    return html.Div([html.Div(entry) for entry in reversed(log_entries)])


def _render_active_events(active_events: list, sim_step: int) -> html.Div:
    """The injector panel's "still active" readout — mirrors the original
    Streamlit render_sidebar_status, minus session_state: steps-remaining
    counts down as sim_step advances since this re-renders every tick."""
    if not active_events:
        return html.Div("None — clean replay.", style={"fontSize": "0.85em"})
    rows = []
    for event in active_events:
        remaining = max(event.duration - (sim_step - event.trigger_step), 0)
        label = event.scenario
        if event.target_sensor:
            label += f" @ {event.target_sensor}"
        rows.append(html.Div(f"{label} — magnitude {event.magnitude:g}, {remaining} steps left", style={"fontSize": "0.85em"}))
    return html.Div(rows)


def _render_top_bar_injector_slot(active_events: list) -> str:
    if not active_events:
        return ""
    return f"{len(active_events)} active injected event(s)"


@app.callback(
    Output(MARKER_LAYER_ID, "children"),
    Output("overall-risk-display", "children"),
    Output("current-readings-panel", "children"),
    Output("rule-eval-panel", "children"),
    Output("all-sensor-status-panel", "children"),
    Output("event-log-content", "children"),
    Output("event-log-store", "data"),
    Output("sensor-status-store", "data"),
    Output("injector-active-events-display", "children"),
    Output("top-bar-injector-slot", "children"),
    Input("sim-step-store", "data"),
    Input("selected-sensor-store", "data"),
    Input("active-events-store", "data"),
    State("event-log-store", "data"),
    State("sensor-status-store", "data"),
)
def update_risk_fanout(sim_step, selected_sensor, active_events_raw, log_entries, previous_categories):
    """Runs risk_assessment.assess_risk once per tick and fans its single
    result out to every display that depends on it — the map, top bar,
    readings/rule-eval panels, all-sensor summary, and event log all read
    fields off the same `assessment`; none of them recompute risk.

    Re-derives the injected reading frame the same way advance_replay does,
    rather than reading a shared Store: a full DataFrame isn't something
    worth round-tripping through JSON every tick, and apply_injections is
    pure, so calling it again here with the same (already-pruned)
    active_events is exactly as correct as sharing one result would be —
    just a few extra microseconds of pandas filtering on a 10k-row frame.
    """
    active_events = events_from_store(active_events_raw)
    visible = visible_readings(READINGS, TIMELINE, sim_step)
    injected, _ = apply_injections(visible, TIMELINE, active_events, sim_step)

    assessment = assess_risk(injected, SENSORS_META)

    markers = build_markers(SENSORS_META, assessment.sensors)
    overall_risk_display = _render_overall_risk(assessment.overall_state)

    selected_assessment = assessment.sensors.get(selected_sensor)
    rainfall_latest = latest_value(injected, selected_sensor, "rainfall_intensity")
    soil_latest, soil_is_catchment = _latest_soil_moisture(injected, selected_sensor)
    readings_panel = _render_current_readings(selected_assessment, rainfall_latest, soil_latest, soil_is_catchment)
    rule_eval_panel = _render_rule_eval(selected_assessment)

    all_sensor_panel = _render_all_sensor_status(assessment)

    new_categories, new_log_entries = _update_event_log(assessment, previous_categories, log_entries)
    event_log_display = _render_event_log(new_log_entries)

    active_events_display = _render_active_events(active_events, sim_step)
    top_bar_injector_slot = _render_top_bar_injector_slot(active_events)

    return (
        markers,
        overall_risk_display,
        readings_panel,
        rule_eval_panel,
        all_sensor_panel,
        event_log_display,
        new_log_entries,
        new_categories,
        active_events_display,
        top_bar_injector_slot,
    )


if __name__ == "__main__":
    # 8050 (Dash's default) collides with an unrelated project's leftover
    # server on this machine — a dedicated port avoids that fight entirely.
    app.run(debug=True, port=8060)
