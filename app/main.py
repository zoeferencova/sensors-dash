"""Dash entry point — Steps 3-4 of the Streamlit->Dash port.

Step 3 (CLAUDE.md "Dashboard sections (target layout)") built the map, the
sensor charts, and the structural layout. Step 4 (CLAUDE.md "The alert /
nowcasting logic") wires the already-ported `risk_assessment.assess_risk`
into that layout: one callback (`update_risk_fanout`) runs it once per tick
and fans the single result out to the map pins, the top-bar overall state,
the current-readings and rule-evaluation panels, the sensor tabs' status
dots, and the event log. Nothing downstream recomputes risk itself — they only
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
  no re-render, no teardown. The x-axis is a sliding window that follows
  the replay clock, moved each tick by a client-side Plotly.relayout
  (slide_chart_x_range) so that the figure prop itself is still never
  touched on a tick.
"""

from datetime import timedelta

import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

import charts
from constants import NEUTRAL_PIN_COLOR, SEVERITY_COLORS, STAGES
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
from sensor_map import (
    FAULT_STROKE_COLOR,
    MARKER_ID_TYPE,
    MARKER_LAYER_ID,
    SELECTED_RING_COLOR,
    build_map,
    build_markers,
)

READINGS = clean_readings(load_long())
SENSORS_META = load_sensors()
TIMELINE = build_timeline(READINGS)
SENSOR_IDS = [s["sensor_id"] for s in SENSORS_META]
DEFAULT_SENSOR = SENSOR_IDS[0]

# How much history the charts show at once. The x-axis is a sliding window
# ending at the current replay moment — [now - CHART_WINDOW, now] — that
# scrolls forward as replay plays (Flood-Hub style), rather than being
# pinned to the whole ~3-day replay span (which squeezed every event into a
# few unreadable pixels). This is the one knob for how much history is
# visible; nothing else needs changing to widen or narrow the view.
CHART_WINDOW = timedelta(hours=12)


def chart_x_range(sim_step: int) -> list[str]:
    """The charts' x-axis range at `sim_step`: a CHART_WINDOW-wide window
    ending at the current replay moment.

    The window is always exactly CHART_WINDOW wide — the start is NOT
    clamped to the beginning of the timeline. Clamping would make the
    window grow during the first CHART_WINDOW of playback, which is the
    axis-rescaling-under-the-data problem the fixed range exists to avoid;
    an initially part-empty window that fills in left-to-right keeps the
    time scale constant from the first tick.

    Returned as ISO strings for the same reason trace data uses .tolist():
    keep what reaches the client plain JSON, never pandas/numpy objects.
    """
    now = TIMELINE[max(0, min(sim_step, len(TIMELINE) - 1))]
    return [(now - CHART_WINDOW).isoformat(), now.isoformat()]


# --- Sensor status dots + selectable sensor tabs --------------------------

SENSOR_TAB_ID_TYPE = "sensor-tab"
SENSOR_TAB_DOT_ID_TYPE = "sensor-tab-dot"


def _dot_style(fill: str, ring_color: str | None = None, dashed: bool = False, size: str = "9px") -> dict:
    """One status dot, drawn the way the map draws the matching pin: filled
    by severity, ringed in its own fill color for an ordinary reading and in
    a dashed FAULT_STROKE_COLOR for a possible_fault.

    content-box sizing means the fill keeps the same diameter whichever ring
    is drawn, so a row of dots doesn't jitter as statuses change under it.
    """
    return {
        "display": "inline-block",
        "width": size,
        "height": size,
        "borderRadius": "50%",
        "backgroundColor": fill,
        "flex": "0 0 auto",
        "border": f"2px {'dashed' if dashed else 'solid'} {ring_color or fill}",
        "boxSizing": "content-box",
    }


def _status_dot_style(sensor_assessment) -> dict:
    """The dot for one sensor, derived from its assessment by the same
    fill/ring rules sensor_map._marker_style applies to that sensor's pin —
    so a tab and its map pin can never disagree about what a sensor is
    doing. No assessment yet falls back to the same neutral color the map
    uses before the first fanout."""
    if sensor_assessment is None:
        return _dot_style(NEUTRAL_PIN_COLOR)
    if sensor_assessment.possible_fault:
        return _dot_style(SEVERITY_COLORS[sensor_assessment.threshold_state], FAULT_STROKE_COLOR, dashed=True)
    return _dot_style(SEVERITY_COLORS[sensor_assessment.effective_state])


def _tab_class(sensor_id: str, selected_sensor: str) -> str:
    return "sensor-tab sensor-tab-selected" if sensor_id == selected_sensor else "sensor-tab"


def build_sensor_tabs(selected_sensor: str) -> list[html.Button]:
    """The four sensor tabs — status display AND sensor selector in one
    control (they replaced both the old dropdown and the separate
    all-sensor-status panel).

    Built ONCE into the static layout and never re-created: a tick only
    rewrites the dots' `style` and the buttons' `className` (see
    update_risk_fanout). Keeping the Button components themselves in place
    is what lets their n_clicks counters accumulate normally — a per-tick
    rebuild would reset n_clicks and make every tick look like a click on
    the first tab, which is exactly the trap the map's markers need a guard
    for in select_sensor.
    """
    return [
        html.Button(
            id={"type": SENSOR_TAB_ID_TYPE, "index": sensor["sensor_id"]},
            className=_tab_class(sensor["sensor_id"], selected_sensor),
            title=f"{sensor['sensor_id']} — {sensor['name']}",
            n_clicks=0,
            children=[
                html.Span(
                    id={"type": SENSOR_TAB_DOT_ID_TYPE, "index": sensor["sensor_id"]},
                    style=_dot_style(NEUTRAL_PIN_COLOR),
                ),
                html.Span(sensor["sensor_id"]),
            ],
        )
        for sensor in SENSORS_META
    ]


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
    className="top-bar",
    children=[
        # LEFT — title
        html.H1("Botič Flood Monitoring", className="dashboard-title"),

        # CENTER — clock + playback controls
        html.Div(
            className="top-bar-center",
            children=[
                html.Div(id="clock-display"),
                html.Div(
                    className="playback-controls",
                    children=[
                        html.Button("Pause", id="play-pause-btn", className="btn btn-primary", n_clicks=0),
                        dcc.RadioItems(
                            id="speed-control",
                            options=SPEED_OPTIONS,
                            value=1000,
                            inline=True,
                            className="speed-segmented",
                        ),
                    ],
                ),
            ],
        ),

        # RIGHT — risk status
        html.Div(
            className="top-bar-right",
            children=[
                html.Div(id="overall-risk-display"),
                # injector slot kept here for the "N active events" global bit
                html.Div(id="top-bar-injector-slot"),
            ],
        ),
    ],
)

# --- Left panel ---------------------------------------------------------

left_panel_top = html.Div(
    id="left-panel-top",
    style={"flex": "1 1 auto", "overflowY": "auto", "padding": "8px"},
    children=[
        html.H4("Sensor Status", style={"marginTop": "4px"}),
        html.Div(id="sensor-tabs", className="sensor-tabs", children=build_sensor_tabs(DEFAULT_SENSOR)),
        # A real (if empty) Figure rather than {} — Plotly.js otherwise logs
        # a harmless but noisy "doesn't yet have a plot" warning on first
        # paint, before update_charts's initial call replaces it moments
        # later anyway.
        dcc.Graph(id="water-level-graph", figure=go.Figure()),
        dcc.Graph(id="rainfall-graph", figure=go.Figure()),
        html.Div(id="current-readings-panel"),
        # <details>/<summary> gives a native collapsible with no callback and
        # no extra dependency. The Details/Summary wrapper lives HERE in the
        # static layout rather than inside _render_rule_eval's output: the
        # rule-eval content is re-rendered every tick, so if the <details>
        # element itself were rebuilt each time, the browser would drop its
        # open/closed state and the panel would snap shut once per tick.
        # Only the inner Div's children change, so the disclosure state is
        # the user's to keep.
        html.Details(
            id="rule-eval-accordion",
            style={"marginTop": "8px"},
            children=[
                html.Summary("Rule evaluation", style={"cursor": "pointer", "fontWeight": "bold"}),
                html.Div(id="rule-eval-panel"),
            ],
        ),
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
    children=[left_panel_top],
)

# --- Center: map ----------------------------------------------------------

map_panel = html.Div(id="map-panel", style={"flex": "1 1 auto", "minWidth": 0}, children=[build_map(SENSORS_META)])

# --- Right panel ----------------------------------------------------------

DEFAULT_TARGET_SENSOR = UPSTREAM_SENSOR_ID if UPSTREAM_SENSOR_ID in SENSOR_IDS else DEFAULT_SENSOR

injector_panel = html.Div(
    id="injector-placeholder",
    style={
        "padding": "8px",
        "margin": "8px",
    },
    children=[
        html.Div(
            style={"display": "flex", "alignItems": "center", "gap": "6px"},
            children=[
                html.H4("Event injector"),
                # title= renders as the browser's native tooltip on hover —
                # no dbc dependency and no callback needed for what is
                # purely explanatory text.
                html.Span(
                    "ⓘ",
                    title=(
                        "Overlays synthetic readings onto the stream assess_risk sees. "
                        "It never sets the risk state directly, and never touches the "
                        "underlying dataset — Reset always returns to a clean replay."
                    ),
                    style={"cursor": "help", "color": "#b35900"},
                ),
            ],
        ),
        html.Label("Scenario", className="field-label"),
        dcc.Dropdown(
            id="injector-scenario-select",
            options=[{"label": s, "value": s} for s in SCENARIOS],
            value=SCENARIOS[0],
            clearable=False,
        ),
        html.Div(id="injector-scenario-description"),
        html.Div(
            id="injector-target-sensor-row",
            children=[
                html.Label("Target sensor", className="field-label"),
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
                html.Button("Trigger event", id="injector-trigger-btn", className="btn btn-primary", n_clicks=0),
                html.Button("Reset", id="injector-reset-btn", className="btn btn-secondary", n_clicks=0),
            ],
        ),
        html.Div(id="injector-active-events-display", style={"marginTop": "8px"}),
    ],
)

def _legend_row(fill: str, label: str, ring_color: str | None = None, dashed: bool = False) -> html.Div:
    """One legend entry. The swatch is _dot_style — the exact same circle the
    sensor tabs draw — so the legend explains the tabs and the map pins with
    one shared definition rather than a lookalike."""
    swatch = dict(_dot_style(fill, ring_color, dashed, size="10px"), marginRight="6px")
    return html.Div(
        style={"display": "flex", "alignItems": "center", "marginBottom": "2px"},
        children=[html.Span(style=swatch), html.Span(label)],
    )


legend_panel = html.Div(
    id="status-legend",
    style={"padding": "8px", "margin": "8px", "borderTop": "2px solid #999", "fontSize": "0.75em", "color": "#444"},
    children=[
        html.Div("Legend", style={"fontWeight": "bold", "marginBottom": "3px"}),
        # Severity rows come from constants.STAGES/SEVERITY_COLORS rather than
        # a hardcoded list, so the legend can't drift from the palette the
        # map and panels actually use.
        *[_legend_row(SEVERITY_COLORS[stage], stage) for stage in STAGES],
        # The two that aren't self-explanatory: a dashed ring means high water
        # with no confirming upstream rain, so it is NOT escalated; a solid
        # accent ring is the map's marker for whichever sensor the left panel
        # is currently showing.
        _legend_row(SEVERITY_COLORS["Watch"], "Possible fault", ring_color=FAULT_STROKE_COLOR, dashed=True),
        _legend_row(SEVERITY_COLORS["Normal"], "Selected sensor", ring_color=SELECTED_RING_COLOR),
    ],
)

event_log_placeholder = html.Div(
    id="event-log-placeholder",
    style={"padding": "8px", "margin": "8px", "borderTop": "2px solid #999"},
    children=[
        html.H4("Event log"),
        html.Div(id="event-log-content"),
    ],
)

right_panel = html.Div(
    id="right-panel",
    style={
        "width": "300px",
        "flex": "0 0 300px",
        "display": "flex",
        "flexDirection": "column",
        "minHeight": 0,
        "overflowY": "auto",
    },
    children=[injector_panel, legend_panel, event_log_placeholder],
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
        # [start, end] ISO strings for the charts' sliding x-axis window,
        # written by advance_replay and consumed ONLY by the clientside
        # relayout below — see slide_chart_x_range for why it doesn't ride
        # along with the chart callback's extendData.
        dcc.Store(id="chart-x-range-store", data=None),
        # Write-only sink: a clientside callback needs an Output, and this
        # one's whole effect is the Plotly.relayout it performs.
        dcc.Store(id="chart-x-range-applied", data=None),
        # Append-only log entries (strings), newest kept up to MAX_LOG_ENTRIES.
        dcc.Store(id="event-log-store", data=[]),
        # Previous tick's per-sensor category (confirmed_flood/possible_fault/
        # normal) — used only to detect transitions for the event log; never
        # displayed directly. Empty dict means "no history yet" (page load).
        dcc.Store(id="sensor-status-store", data={}),
        # disabled=True: replay opens paused at the first timestep, per
        # CLAUDE.md — history builds up as it plays, not fully populated.
        dcc.Interval(id="replay-interval", interval=1000, n_intervals=0, disabled=False),
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
    Output("chart-x-range-store", "data"),
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
    n_intervals (modulo the timeline length, so playback loops) and is
    recomputed the same way regardless of which Input fired, since Trigger
    needs "now" as the new event's trigger_step.

    Also emits the charts' sliding x-axis window, for one specific reason:
    the window has to reach the client in a DIFFERENT response than the
    charts' extendData, and this callback is the one that runs strictly
    before update_charts (which is triggered by the sim-step-store this
    writes). See slide_chart_x_range.
    """
    # Modulo, not clamp: replay loops back to the start and keeps playing
    # instead of stalling at the last timestep. n_intervals only advances
    # while the Interval is enabled, so pausing doesn't skip the clock
    # forward and the wrap point stays exactly len(TIMELINE) ticks apart.
    sim_step = n_intervals % len(TIMELINE)
    triggered = ctx.triggered_id

    if triggered == "play-pause-btn":
        now_disabled = not is_disabled
        return no_update, no_update, no_update, no_update, now_disabled, ("Play" if now_disabled else "Pause")

    if triggered == "injector-reset-btn":
        # active_events_raw is already the raw (dict) list — no need to
        # deserialize just to check emptiness. Skips a spurious Store write
        # (and the callbacks it would re-trigger) when there was nothing to
        # clear.
        events_output = no_update if not active_events_raw else []
        return no_update, events_output, no_update, no_update, no_update, no_update

    active_events = events_from_store(active_events_raw)

    if triggered == "injector-trigger-btn":
        new_event = build_event(scenario, target_sensor, DEFAULT_MAGNITUDE[scenario], sim_step)
        return no_update, events_to_store(active_events + [new_event]), no_update, no_update, no_update, no_update

    # Default path: a replay-interval tick (or the initial page-load call).
    # Never fires while paused, since a disabled dcc.Interval simply doesn't
    # tick.
    visible = visible_readings(READINGS, TIMELINE, sim_step)
    _injected, still_active = apply_injections(visible, TIMELINE, active_events, sim_step)

    current_time = TIMELINE[sim_step]
    display = html.Div(
        className="clock-block",
        children=[
            html.Span(current_time.strftime("%b %d, %Y · %H:%M"), className="clock-time"),
            html.Span(f"{sim_step} / {len(TIMELINE) - 1}", className="clock-step"),
        ],
    )

    # Only write active-events-store when an event actually expired this
    # tick — writing a freshly-reserialized-but-value-identical list every
    # tick regardless would spuriously re-trigger every callback that Inputs
    # on this Store (update_charts, update_risk_fanout) on EVERY tick, not
    # just when something real changed. That doubled invocation rate is
    # exactly what widens the window for response-ordering races (e.g. a
    # sensor switch landing right on a tick can otherwise show the
    # previous sensor's chart title).
    #
    # On wraparound, drop any injected events outright. Their trigger_step
    # is an index into the run that just ended, so after the wrap
    # `sim_step - trigger_step` is negative — apply_injections' expiry check
    # can never fire again, and the pulse would instead reappear at the same
    # step index on every subsequent loop. Clearing them is also what makes
    # the loop mean "start fresh": the next pass is a clean replay, exactly
    # like pressing Reset.
    wrapped = n_intervals > 0 and sim_step == 0
    if wrapped:
        still_active = []

    events_unchanged = events_signature(still_active) == events_signature(active_events)
    events_output = no_update if events_unchanged else events_to_store(still_active)

    return sim_step, events_output, display, chart_x_range(sim_step), no_update, no_update


# The sliding window is applied with a direct Plotly.relayout on the graph
# div, deliberately NOT by sending a new (or Patched) `figure` prop.
#
# dcc.Graph queues an extendData append in component state, and its
# UNSAFE_componentWillReceiveProps starts that queue from EMPTY_DATA
# whenever `figure` changed too — then guards the setState with an identity
# check against that same EMPTY_DATA constant. So if `figure` and
# `extendData` for one graph arrive in the SAME response, the append is
# dropped without any error: the chart just quietly stops updating. (It
# also pushes the dropped entry into the shared EMPTY_DATA array, so the
# damage can resurface on a later, unrelated append.) That holds for a
# Patch too — a Patch still changes the figure prop's identity.
#
# Hence the split: extendData rides update_charts, the range rides
# advance_replay, and they land in two separate responses. The order is
# guaranteed rather than lucky — update_charts is triggered BY the
# sim-step-store advance_replay writes, so the relayout has always finished
# by the time that tick's extendData arrives. (dcc syncs gd.layout back
# into the figure prop after any relayout; keeping that sync in its own
# response is exactly what stops it colliding with an append.)
app.clientside_callback(
    """
    function slide_chart_x_range(xRange) {
        if (!xRange) { return window.dash_clientside.no_update; }
        ["water-level-graph", "rainfall-graph"].forEach(function (graphId) {
            var container = document.getElementById(graphId);
            var gd = container && container.querySelector(".js-plotly-plot");
            // _fullLayout is Plotly's "this div has been plotted" marker —
            // the async graph bundle may still be loading on first paint.
            if (!gd || !gd._fullLayout) { return; }
            var applied = xRange[0] + "|" + xRange[1];
            // Plotly rewrites date ranges into its own string format, so
            // gd.layout can't be compared against what we sent; track the
            // last applied window on the element instead. Skips a no-op
            // relayout (and the figure-prop sync it would trigger) while
            // replay is paused.
            if (gd.__lastXRange === applied) { return; }
            gd.__lastXRange = applied;
            window.Plotly.relayout(gd, {"xaxis.range": [xRange[0], xRange[1]]});
        });
        return window.dash_clientside.no_update;
    }
    """,
    Output("chart-x-range-applied", "data"),
    Input("chart-x-range-store", "data"),
    prevent_initial_call=True,
)


@app.callback(
    Output("selected-sensor-store", "data"),
    Input({"type": SENSOR_TAB_ID_TYPE, "index": ALL}, "n_clicks"),
    Input({"type": MARKER_ID_TYPE, "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_sensor(_tab_clicks, _marker_clicks):
    """selected-sensor-store is the single source of truth for "which sensor
    is the left panel showing", and this is the only callback that writes
    it. Both entry points — a sensor tab and a map pin — are
    pattern-matching ids of the same shape, so whichever fired is read off
    ctx.triggered_id["index"] identically; there is no second control
    holding its own copy of the selection that could drift out of sync
    (which is what the old dropdown did whenever a pin was clicked).

    The n_clicks check is load-bearing, not defensive: update_risk_fanout
    rebuilds the marker LayerGroup's children EVERY tick (to recolor pins by
    status), which hands Dash brand-new CircleMarker components whose
    n_clicks is unset. Dash counts that as a change on this ALL-input, so
    this callback fires on every tick with a marker as triggered_id even
    though nobody touched the map — and honouring it there would reset the
    selection to that marker (S01, the first) once per tick, silently
    undoing whatever sensor the user picked. A rebuild carries a falsy
    n_clicks; only a real click carries a count. (The tabs are immune by
    construction — they're never rebuilt, only restyled — but the same
    guard covers them for free.)
    """
    triggered = ctx.triggered_id
    if not isinstance(triggered, dict):
        return no_update
    clicked = ctx.triggered[0].get("value") if ctx.triggered else None
    if not clicked:
        return no_update
    return triggered["index"]


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


def _stored_events_signature(active_events: list) -> list:
    """events_signature's fingerprint, in the shape it comes back as after
    a round-trip through chart-state-store.

    The store serializes to JSON, which turns the nested tuples into nested
    LISTS — so comparing the value read back out against a freshly computed
    tuple never matches, not even when both describe the same set (`() !=
    []` for the common case of no active events at all). That made
    `events_changed` permanently True, which silently routed EVERY tick
    down the full-figure rebuild branch and left the extendData path dead
    code. Normalizing to the JSON shape on this side of the store boundary
    is what makes the comparison mean what it reads as.
    """
    return [list(item) for item in events_signature(active_events)]


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
    change, the active-events set actually changing, OR the replay looping
    back to the start, rebuild both figures from scratch with the full
    history visible so far (all three are infrequent — a full figure resend
    is fine). On every other call (a replay tick, same sensor, same events),
    only the row(s) newer than what's already drawn are sent via extendData.

    The loop case has to rebuild rather than extend: after a wrap the
    "newer than what's drawn" slice is empty, so extending would leave the
    previous pass's whole accumulated trace frozen on screen while the
    x-axis jumped back to the start — the traces would simply vanish off
    the right-hand edge. Rebuilding at step 0 clears them and starts the
    new pass from a single point, which is what makes a loop read as a
    fresh start rather than a glitch.

    A just-triggered event needs the full-rebuild path even though it
    doesn't change sim_step: build_event's trigger_step is "now", and
    _pulse returns a nonzero factor at relative_step=0 — so the CURRENTLY
    visible point's value changes retroactively the instant an event is
    triggered (or reverts the instant one expires/is reset). A signature
    over the active set (not just its length) catches both directions.

    This callback must NEVER write `figure` and `extendData` for the same
    graph in the same response — not even a Patch. dcc.Graph drops the
    append when both arrive together (see slide_chart_x_range for the
    mechanism), so a tick that also touched the figure would silently stop
    the chart updating. The two branches below are mutually exclusive for
    exactly that reason, and the sliding x-axis window is deliberately
    routed around this callback entirely: it rides on chart-x-range-store
    instead.
    """
    active_events = events_from_store(active_events_raw)
    visible = visible_readings(READINGS, TIMELINE, sim_step)
    injected, _ = apply_injections(visible, TIMELINE, active_events, sim_step)
    water_level_series = get_series(injected, selected_sensor, "water_level")
    rainfall_series = get_series(injected, selected_sensor, "rainfall_intensity")

    current_signature = _stored_events_signature(active_events)
    last_rendered_step = chart_state.get("rendered_upto_step", -1)
    sensor_changed = chart_state.get("sensor_id") != selected_sensor
    events_changed = chart_state.get("events_signature") != current_signature
    # The replay clock only ever moves backwards by wrapping (advance_replay
    # takes n_intervals modulo the timeline length), so "this step is older
    # than what's drawn" IS the loop signal — no extra store needed to
    # communicate it.
    wrapped = sim_step < last_rendered_step
    new_state = {
        "sensor_id": selected_sensor,
        "rendered_upto_step": sim_step,
        "events_signature": current_signature,
    }

    if sensor_changed or events_changed or wrapped:
        # The rebuilt figure carries the CURRENT window, not the timeline's
        # start — otherwise switching sensors mid-replay would snap both
        # charts back to the beginning until the next tick nudged them
        # forward. This is the only place the range travels with a figure;
        # every tick moves it via relayout instead.
        x_range = chart_x_range(sim_step)
        water_level_fig = charts.build_water_level_figure(selected_sensor, x_range)
        charts.update_water_level_figure(water_level_fig, water_level_series)
        rainfall_fig = charts.build_rainfall_figure(selected_sensor, x_range)
        charts.update_rainfall_figure(rainfall_fig, rainfall_series)
        return water_level_fig, no_update, rainfall_fig, no_update, new_state

    # Only sim_step == last_rendered_step reaches here (a re-fire on the same
    # step, e.g. while paused); the backwards case was handled as a wrap.
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
    text_color = "#fff"
    return html.Span(
        f"Overall risk: {overall_state}",
        style={
            "backgroundColor": color,
            "color": text_color,
            "padding": "6px 12px",
            "borderRadius": "6px",
            "fontWeight": 500,
            "fontSize": "0.95em"
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
    """Body only — the "Rule evaluation" heading is the accordion's
    <summary> in the static layout, so it must not be repeated here."""
    if sensor_assessment is None:
        return html.Div("No data yet.")

    conditions = sensor_assessment.conditions

    def mark(flag: bool) -> str:
        return "✓" if flag else "✗"

    return html.Div(
        [
            html.Div(f"water_level ≥ Watch: {mark(conditions['water_level_watch_plus'])}"),
            html.Div(f"rising_fast: {mark(conditions['rising_fast'])}"),
            html.Div(f"upstream_rain_confirmed: {mark(conditions['upstream_rain_confirmed'])}"),
            html.Div(html.B(f"→ {_verdict_label(sensor_assessment)}")),
        ]
    )


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
        return html.Div("Status: no simulated events active", className="injector-status")
    rows = []
    for event in active_events:
        remaining = max(event.duration - (sim_step - event.trigger_step), 0)
        label = event.scenario
        if event.target_sensor:
            label += f" @ {event.target_sensor}"
        rows.append(html.Div(f"{label} — magnitude {event.magnitude:g}, {remaining} steps left", className="injector-status"))
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
    # The sensor tabs are restyled, never rebuilt — see build_sensor_tabs.
    # Dash resolves these ALL-outputs in the same order it resolves the
    # matching ALL-inputs (sorted by id), which for {"index": "S01".."S04"}
    # is SENSORS_META's own order — so both lists are built by iterating
    # SENSOR_IDS and line up positionally.
    Output({"type": SENSOR_TAB_DOT_ID_TYPE, "index": ALL}, "style"),
    Output({"type": SENSOR_TAB_ID_TYPE, "index": ALL}, "className"),
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
    readings/rule-eval panels, the sensor tabs' status dots, and the event
    log all read fields off the same `assessment`; none of them recompute
    risk.

    Selection is fanned out from here too (the tabs' `className` and the
    map's selection halo), because `selected-sensor-store` is already an
    Input: the tab highlight and the map ring are therefore written in the
    same response, and can't disagree even for a frame.

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

    markers = build_markers(SENSORS_META, assessment.sensors, selected_sensor)
    overall_risk_display = _render_overall_risk(assessment.overall_state)

    tab_dot_styles = [_status_dot_style(assessment.sensors.get(sid)) for sid in SENSOR_IDS]
    tab_classes = [_tab_class(sid, selected_sensor) for sid in SENSOR_IDS]

    selected_assessment = assessment.sensors.get(selected_sensor)
    rainfall_latest = latest_value(injected, selected_sensor, "rainfall_intensity")
    soil_latest, soil_is_catchment = _latest_soil_moisture(injected, selected_sensor)
    readings_panel = _render_current_readings(selected_assessment, rainfall_latest, soil_latest, soil_is_catchment)
    rule_eval_panel = _render_rule_eval(selected_assessment)

    new_categories, new_log_entries = _update_event_log(assessment, previous_categories, log_entries)
    event_log_display = _render_event_log(new_log_entries)

    active_events_display = _render_active_events(active_events, sim_step)
    top_bar_injector_slot = _render_top_bar_injector_slot(active_events)

    return (
        markers,
        overall_risk_display,
        readings_panel,
        rule_eval_panel,
        tab_dot_styles,
        tab_classes,
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
