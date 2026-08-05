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
    steps_remaining,
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


# The injected readings frame for one moment of the replay, memoized on
# (sim_step, active events). update_charts and update_risk_fanout both need
# exactly this frame for the same tick and used to build it independently —
# two full copies of a frame that grows with the replay, per tick. Caching
# one entry collapses that to one build and roughly halves what a tick costs
# downstream, which is the headroom that keeps the chain comfortably inside
# the tick period.
#
# Read-only by contract: callers pass it to get_series/latest_value/
# assess_risk, none of which mutate. Anything that needs to modify it must
# take its own copy.
_INJECTED_CACHE: dict = {"key": None, "frame": None}


def injected_readings(sim_step: int, active_events: list):
    """visible_readings + apply_injections for `sim_step`, memoized.

    Both inputs are captured in the key, and apply_injections is pure, so a
    hit is indistinguishable from recomputing. A single entry is enough: the
    two callers run back-to-back on the same tick. (Module-global, so two
    browsers on different steps would simply miss rather than get a wrong
    frame — this is a single-user demo dashboard.)

    RETENTION. Events are never dropped from `active_events` except by
    Reset, which is what keeps an injected spike in the chart's history
    instead of vanishing the moment the event finishes. This works without
    any extra machinery because apply_injections keys the overlay on each
    ROW's own timestep, not on "now": `_pulse` is nonzero only for rows
    inside `[trigger_step + lag, trigger_step + lag + rise+hold+decay)`, and
    apply_injections masks on `factor > 0`. So a retained event keeps
    modifying exactly the rows it always modified, and leaves every later
    row alone.

    That is also why retaining an event does NOT pin the risk state high
    forever: assess_risk reads the CURRENT step, whose rows are past the
    pulse and therefore untouched, so the catchment escalates and then
    recovers on its own while the spike stays drawn behind it. The injector
    still only feeds inputs to the rules, exactly as CLAUDE.md requires —
    retention changes how long an input is remembered, never who decides.
    """
    key = (sim_step, events_signature(active_events))
    if _INJECTED_CACHE["key"] != key:
        visible = visible_readings(READINGS, TIMELINE, sim_step)
        injected, _ = apply_injections(visible, TIMELINE, active_events, sim_step)
        _INJECTED_CACHE["key"] = key
        _INJECTED_CACHE["frame"] = injected
    return _INJECTED_CACHE["frame"]


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


def _status_dot_style(sensor_assessment, size: str = "9px") -> dict:
    """The dot for one sensor, derived from its assessment by the same
    fill/ring rules sensor_map._marker_style applies to that sensor's pin —
    so a tab, the status row, the rule verdict and its map pin can never
    disagree about what a sensor is doing. No assessment yet falls back to
    the same neutral color the map uses before the first fanout."""
    if sensor_assessment is None:
        return _dot_style(NEUTRAL_PIN_COLOR, size=size)
    if sensor_assessment.possible_fault:
        return _dot_style(
            SEVERITY_COLORS[sensor_assessment.threshold_state], FAULT_STROKE_COLOR, dashed=True, size=size
        )
    return _dot_style(SEVERITY_COLORS[sensor_assessment.effective_state], size=size)


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


# The replay clock ticks at a FIXED rate and speed changes how many timeline
# steps each tick advances — it does NOT shorten the tick period.
#
# Speed used to be the Interval's period (1x=1000ms ... 8x=125ms), which
# broke every speed above 1x. One tick fans out into the full callback chain
# (advance_replay -> update_charts + update_risk_fanout), and that chain
# takes a few hundred ms. Once the tick period dropped below the time a
# callback takes, every tick re-triggered advance_replay while the previous
# invocation was still in flight; Dash discards the superseded response, so
# its outputs never committed, sim-step-store never moved, and nothing
# downstream ever ran. Measured at 8x: advance_replay invoked 7.8x/second,
# update_charts invoked ZERO times.
#
# A fixed period can't outrun the chain no matter which speed is selected,
# and the per-speed timeline rate is unchanged from what the labels always
# meant (1x = 1 step/second, 8x = 8 steps/second) — 8x now arrives as one
# 8-step advance per second instead of eight advances that never landed.
TICK_MS = 1000

SPEED_OPTIONS = [
    {"label": "1x", "value": 1},
    {"label": "2x", "value": 2},
    {"label": "4x", "value": 4},
    {"label": "8x", "value": 8},
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
                            value=1,
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
    className="left-panel-top",
    children=[
        html.Div("Sensor Status", className="section-label"),
        html.Div(id="sensor-tabs", className="sensor-tabs", children=build_sensor_tabs(DEFAULT_SENSOR)),
        # Readings sit ABOVE the charts: they're the "what is it right now"
        # answer, and the charts are the supporting history. The panel's own
        # children are rendered per tick by update_risk_fanout.
        #
        # Sub-headings inside a section use the same small label the form
        # controls do ("Scenario", "Target sensor"), so the left panel has
        # the same two-level heading hierarchy as the right one.
        html.Div("Current readings", className="field-label sub-label"),
        html.Div(id="current-readings-panel"),
        # A real (if empty) Figure rather than {} — Plotly.js otherwise logs
        # a harmless but noisy "doesn't yet have a plot" warning on first
        # paint, before update_charts's initial call replaces it moments
        # later anyway.
        #
        # Each chart sits in a .chart-slot whose height comes from the CSS.
        # The slot is needed because responsive=True makes dcc.Graph put an
        # inline `height: 100%` on its own wrapper, which outranks any class
        # rule — so the height has to be set on a parent for the Graph to
        # fill. Sizing this way keeps height out of the Figure's layout, so
        # charts.py still builds structure-only figures and a resize stays
        # layout-only (Plotly.Plots.resize) — the extendData append path is
        # untouched.
        html.Div(className="chart-slot", children=dcc.Graph(id="water-level-graph", responsive=True, figure=go.Figure())),
        html.Div(
            className="chart-slot chart-slot-short",
            children=dcc.Graph(id="rainfall-graph", responsive=True, figure=go.Figure()),
        ),
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
            className="rule-eval",
            # Open by default — it's the panel that shows WHY the system
            # reached its verdict, which is the point of the whole rule
            # engine; hiding it behind a click undersells it.
            open=True,
            children=[
                html.Summary("Rule evaluation", className="field-label sub-label rule-eval-summary"),
                html.Div(id="rule-eval-panel", className="rule-eval-body"),
            ],
        ),
    ],
)

left_panel = html.Div(id="left-panel", className="left-panel", children=[left_panel_top])

# --- Center: map ----------------------------------------------------------

map_panel = html.Div(id="map-panel", className="map-panel", children=[build_map(SENSORS_META)])

# --- Right panel ----------------------------------------------------------

DEFAULT_TARGET_SENSOR = UPSTREAM_SENSOR_ID if UPSTREAM_SENSOR_ID in SENSOR_IDS else DEFAULT_SENSOR

injector_panel = html.Div(
    id="injector-placeholder",
    className="panel-card",
    children=[
        html.Div(
            className="card-title-row",
            children=[
                html.H4("Event injector", className="section-label card-title"),
                # title= renders as the browser's native tooltip on hover —
                # no dbc dependency and no callback needed for what is
                # purely explanatory text.
                html.Span(
                    "ⓘ",
                    className="info-hint",
                    title=(
                        "Overlays synthetic readings onto the stream assess_risk sees. "
                        "It never sets the risk state directly, and never touches the "
                        "underlying dataset — Reset always returns to a clean replay."
                    ),
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
            className="injector-actions",
            children=[
                html.Button("Trigger event", id="injector-trigger-btn", className="btn btn-primary", n_clicks=0),
                html.Button("Reset", id="injector-reset-btn", className="btn btn-secondary", n_clicks=0),
            ],
        ),
        html.Div(id="injector-active-events-display"),
    ],
)


def _legend_item(fill: str, label: str, ring_color: str | None = None, dashed: bool = False) -> html.Div:
    """One legend cell. The swatch is _dot_style — the exact same circle the
    sensor tabs and the event log draw — so the legend explains all three
    (plus the map pins) from one shared definition rather than a lookalike."""
    return html.Div(
        className="legend-item",
        children=[html.Span(className="legend-dot", style=_dot_style(fill, ring_color, dashed, size="9px")), html.Span(label)],
    )


legend_panel = html.Div(
    id="status-legend",
    className="panel-card",
    children=[
        html.H4("Legend", className="section-label card-title"),
        # A grid rather than a stack: six one-line entries in a single column
        # made this card taller than the log above it, for no gain — two
        # columns halve its height and keep every label on one line.
        html.Div(
            className="legend-grid",
            children=[
                # Severity cells come from constants.STAGES/SEVERITY_COLORS rather
                # than a hardcoded list, so the legend can't drift from the palette
                # the map and panels actually use.
                *[_legend_item(SEVERITY_COLORS[stage], stage) for stage in STAGES],
                # The two that aren't self-explanatory: a dashed ring means high
                # water with no confirming upstream rain, so it is NOT escalated;
                # a solid accent ring is the map's marker for whichever sensor the
                # left panel is currently showing.
                _legend_item(SEVERITY_COLORS["Watch"], "Possible fault", ring_color=FAULT_STROKE_COLOR, dashed=True),
                _legend_item(SEVERITY_COLORS["Normal"], "Selected", ring_color=SELECTED_RING_COLOR),
            ],
        ),
    ],
)

event_log_placeholder = html.Div(
    id="event-log-placeholder",
    className="panel-card",
    children=[
        html.H4("Event log", className="section-label card-title"),
        html.Div(id="event-log-content"),
    ],
)

# Injector (control) on top, then the log it drives, then the legend that
# decodes both — most-interactive to most-static, top to bottom.
right_panel = html.Div(
    id="right-panel",
    className="right-panel",
    children=[injector_panel, event_log_placeholder, legend_panel],
)

# --- Assemble ----------------------------------------------------------

body_row = html.Div(id="body-row", className="body-row", children=[left_panel, map_panel, right_panel])

app.layout = html.Div(
    className="app-shell",
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
        # Bumped by Reset. update_risk_fanout Inputs on it purely to learn
        # "a Reset just happened" so it can empty the event log — the
        # active-events-store write alone can't say that, since an empty
        # list is also the state at page load.
        dcc.Store(id="reset-token-store", data=0),
        # Append-only log entries (dicts), newest kept up to MAX_LOG_ENTRIES.
        dcc.Store(id="event-log-store", data=[]),
        # Highest timestep assess_risk has already been run for. The gap
        # between it and the new sim_step is what update_risk_fanout sweeps,
        # so a tick that advances 8 steps still evaluates all 8. -1 means
        # "nothing assessed yet" (page load).
        dcc.Store(id="last-assessed-step-store", data=-1),
        # Previous tick's per-sensor category (confirmed_flood/possible_fault/
        # normal) — used only to detect transitions for the event log; never
        # displayed directly. Empty dict means "no history yet" (page load).
        dcc.Store(id="sensor-status-store", data={}),
        # disabled=True: replay opens paused at the first timestep, per
        # CLAUDE.md — history builds up as it plays, not fully populated.
        # Fixed period — see TICK_MS. `interval` is never a callback Output,
        # so the timer is created once and only ever started/stopped by
        # `disabled`; it can't be reset mid-flight by a speed change.
        dcc.Interval(id="replay-interval", interval=TICK_MS, n_intervals=0, disabled=False),
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
def toggle_play_pause(_clicks, is_disabled):
    """Play/pause is its OWN callback, deliberately not merged into
    advance_replay.

    While it shared advance_replay, a Pause click and a replay tick were the
    same callback — so a click landed as one more invocation of a callback
    the ticks were already re-triggering, and Dash discards a superseded
    in-flight invocation. Above 1x the click's response was reliably thrown
    away before it could commit `disabled`, which is exactly why pause "only
    worked at 1x". Separate callbacks can't supersede each other, so this
    now lands on the first click at any speed.
    """
    now_disabled = not is_disabled
    return now_disabled, ("Play" if now_disabled else "Pause")


@app.callback(
    Output("sim-step-store", "data"),
    Output("active-events-store", "data"),
    Output("reset-token-store", "data"),
    Output("clock-display", "children"),
    Output("chart-x-range-store", "data"),
    Input("replay-interval", "n_intervals"),
    Input("injector-trigger-btn", "n_clicks"),
    Input("injector-reset-btn", "n_clicks"),
    State("sim-step-store", "data"),
    State("speed-control", "value"),
    State("active-events-store", "data"),
    State("reset-token-store", "data"),
    State("injector-scenario-select", "value"),
    State("injector-target-sensor-select", "value"),
)
def advance_replay(
    _n_intervals,
    _trigger_clicks,
    _reset_clicks,
    current_step,
    steps_per_tick,
    active_events_raw,
    reset_token,
    scenario,
    target_sensor,
):
    """Advances the replay clock, and owns active-events-store (written only
    by the injector's Trigger and Reset) — branches on ctx.triggered_id.

    sim_step ACCUMULATES from sim-step-store rather than being derived from
    n_intervals, because speed is now steps-per-tick (see TICK_MS): the tick
    count no longer maps 1:1 to timeline position. The accumulator also
    degrades better — if a tick's response is ever dropped, playback loses
    one step instead of jumping to wherever the free-running n_intervals
    counter had got to.

    The tick path does NO event work at all. Injected events are retained
    until Reset (see injected_readings), so there is nothing to expire and
    nothing to write back — the tick is a modulo, a strftime and a Div.
    That matters because this callback feeds every other one: an earlier
    version called visible_readings + apply_injections here just to compute
    which events had expired, copying the whole growing readings frame every
    step (~190-320ms), which made it slow enough to be superseded by the
    following tick at any speed above 1x — and then nothing downstream of it
    ran at all.

    Also emits the charts' sliding x-axis window, for one specific reason:
    the window has to reach the client in a DIFFERENT response than the
    charts' extendData, and this callback is the one that runs strictly
    before update_charts (which is triggered by the sim-step-store this
    writes). See slide_chart_x_range.
    """
    triggered = ctx.triggered_id
    sim_step = current_step or 0

    if triggered == "injector-reset-btn":
        # Always writes, even when there was nothing injected: the token is
        # what tells update_risk_fanout to clear the event log, and Reset
        # means "clean slate" whether or not an event happened to be
        # retained at the time.
        return no_update, [], (reset_token or 0) + 1, no_update, no_update

    if triggered == "injector-trigger-btn":
        active_events = events_from_store(active_events_raw)
        new_event = build_event(scenario, target_sensor, DEFAULT_MAGNITUDE[scenario], sim_step)
        return no_update, events_to_store(active_events + [new_event]), no_update, no_update, no_update

    # Default path: a replay-interval tick (or the initial page-load call).
    # Never fires while paused, since a disabled dcc.Interval simply doesn't
    # tick. The initial call must NOT advance, or the replay would open on
    # step 1 with step 0 never rendered.
    if triggered is not None:
        sim_step = (sim_step + (steps_per_tick or 1)) % len(TIMELINE)

    current_time = TIMELINE[sim_step]
    display = html.Div(
        className="clock-block",
        children=[
            html.Span(current_time.strftime("%b %d, %Y · %H:%M"), className="clock-time"),
            html.Span(f"{sim_step} / {len(TIMELINE) - 1}", className="clock-step"),
        ],
    )

    # active-events-store is deliberately never written on a tick. Writing a
    # reserialized-but-identical list every tick would spuriously re-trigger
    # every callback that Inputs on it (update_charts, update_risk_fanout)
    # on EVERY tick rather than only when something real changed — the
    # doubled invocation rate is exactly what widens the window for
    # response-ordering races (e.g. a sensor switch landing on a tick
    # showing the previous sensor's chart title).
    #
    # Wraparound no longer clears the events either. A retained event's
    # overlay is keyed on each ROW's timestep, so on the next lap the same
    # rows are injected again and the storm replays where it was put — which
    # is what "retained until Reset" has to mean for a looping replay.
    return sim_step, no_update, no_update, display, chart_x_range(sim_step)


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
    Input("injector-trigger-btn", "n_clicks"),
    State("injector-scenario-select", "value"),
    State("injector-target-sensor-select", "value"),
    prevent_initial_call=True,
)
def select_sensor(_tab_clicks, _marker_clicks, _trigger_clicks, scenario, target_sensor):
    """selected-sensor-store is the single source of truth for "which sensor
    is the left panel showing", and this is the only callback that writes
    it. The two user-driven entry points — a sensor tab and a map pin — are
    pattern-matching ids of the same shape, so whichever fired is read off
    ctx.triggered_id["index"] identically; there is no second control
    holding its own copy of the selection that could drift out of sync
    (which is what the old dropdown did whenever a pin was clicked).

    Firing the injector also selects the sensor the scenario targets, so the
    left panel is already showing the sensor the user is about to watch
    react. Only the TRIGGER does this, not the target dropdown: changing the
    dropdown is the user setting up a scenario, and yanking the charts
    around mid-setup would fight them. The target-sensor value therefore
    rides in as State, not Input.

    "Catchment-wide event" has no single target (it hits all four at their
    own lags), so it selects the upstream boundary gauge — the one the wave
    reaches first, and thus the one worth watching from t=0.

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
    if triggered == "injector-trigger-btn":
        return target_sensor if scenario in SCENARIOS_NEEDING_TARGET else DEFAULT_TARGET_SENSOR
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
    injected = injected_readings(sim_step, active_events)
    water_level_series = get_series(injected, selected_sensor, "water_level")
    rainfall_series = get_series(injected, selected_sensor, "rainfall_intensity")

    current_signature = _stored_events_signature(active_events)
    last_rendered_step = chart_state.get("rendered_upto_step", -1)
    sensor_changed = chart_state.get("sensor_id") != selected_sensor
    events_changed = chart_state.get("events_signature") != current_signature
    # The replay clock only ever moves backwards by wrapping (advance_replay
    # takes its accumulated step modulo the timeline length), so "this step
    # is older than what's drawn" IS the loop signal — no extra store needed
    # to communicate it. Holds for any speed: a multi-step tick that jumps
    # over 0 still lands on a smaller step than the one already drawn.
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
    """Only the fill is inline — it's the one genuinely dynamic bit. Padding,
    radius and type live in .risk-chip."""
    return html.Span(
        f"Overall risk: {overall_state}",
        className="risk-chip",
        style={"backgroundColor": SEVERITY_COLORS[overall_state]},
    )


def _stat_box(value: str, label: str, hint: str | None = None):
    """One stat tile: a number over a small muted label.

    All three tiles are identical — no emphasised variant and no severity
    colour on the number. Water level's stage is already carried by the
    threshold lines on the chart below, the status dots, and the map; a
    fourth encoding of it here only added weight to a panel that needed
    less. Separation is whitespace, not borders.
    """
    return html.Div(
        className="stat-box",
        title=hint,
        children=[
            html.Div(value, className="stat-value"),
            html.Div(label, className="stat-label"),
        ],
    )


def _render_current_readings(sensor_assessment, rainfall_latest, soil_latest, soil_is_catchment) -> html.Div:
    """Three uniform tiles: water level, rainfall, soil moisture. The em dash
    is the no-data value so the tiles keep their shape rather than collapsing
    when a series is missing.

    Detail that used to be inline text — the rainfall "meaningful" flag and
    the soil-moisture CATCHMENT fallback note — lives in the tile's `title`
    tooltip. Both are qualifiers on a number, and at this size the number
    has to stay the thing you read first.
    """
    if sensor_assessment is None:
        return html.Div(className="stat-row", children=[_stat_box("—", "No data yet")])

    water_level = _stat_box(
        f"{sensor_assessment.latest_water_level:.1f}",
        "Water level cm",
        hint=f"ČHMÚ stage: {sensor_assessment.threshold_state}",
    )

    if rainfall_latest is not None:
        _, rain_value = rainfall_latest
        meaningful = rain_value >= RAINFALL_CONFIRM_MM_H
        rainfall = _stat_box(
            f"{rain_value:.1f}",
            "Rainfall mm/h",
            hint=(
                f"≥ {RAINFALL_CONFIRM_MM_H:g} mm/h — meaningful upstream rain for confirmation"
                if meaningful
                else f"Below the {RAINFALL_CONFIRM_MM_H:g} mm/h confirmation threshold"
            ),
        )
    else:
        rainfall = _stat_box("—", "Rainfall mm/h")

    if soil_latest is not None:
        _, soil_value = soil_latest
        soil = _stat_box(
            f"{soil_value:.1f}",
            "Soil moisture %",
            hint="Catchment-wide antecedent wetness" if soil_is_catchment else None,
        )
    else:
        soil = _stat_box("—", "Soil moisture %")

    return html.Div(className="stat-row", children=[water_level, rainfall, soil])


def _condition_row(label: str, met: bool) -> html.Div:
    """One rule condition as a labelled state pill instead of a ✓/✗ glyph.

    Filled (steel) reads as "this fired", outlined as "it didn't" — the same
    filled-vs-outlined distinction the map already uses for a confirmed pin
    versus a dashed unconfirmed one, so the two panels agree on what
    "asserted" looks like.
    """
    return html.Div(
        className="rule-row",
        children=[
            html.Span(label, className="rule-label"),
            html.Span("yes" if met else "no", className=f"rule-pill rule-pill-{'on' if met else 'off'}"),
        ],
    )


def _render_rule_eval(sensor_assessment) -> html.Div:
    """Body only — the "Rule evaluation" heading is the accordion's
    <summary> in the static layout, so it must not be repeated here.

    Condition names stay in the source's own snake_case (CLAUDE.md's
    rule-panel spec names them that way, and so does assess_risk's
    `conditions` dict) — the point of this panel is that you can read it
    against the rules, so prettifying the identifiers would cost more than
    it gains.
    """
    if sensor_assessment is None:
        return html.Div("No data yet.", className="rule-empty")

    conditions = sensor_assessment.conditions

    return html.Div(
        [
            _condition_row("water_level ≥ Watch", conditions["water_level_watch_plus"]),
            _condition_row("rising_fast", conditions["rising_fast"]),
            _condition_row("upstream_rain_confirmed", conditions["upstream_rain_confirmed"]),
            html.Div(
                className="rule-verdict",
                children=[
                    # Same dot vocabulary as the tabs, status row, map pins and
                    # log — a dashed ring here means possible_fault exactly as
                    # it does everywhere else.
                    html.Span(className="rule-verdict-dot", style=_status_dot_style(sensor_assessment, size="8px")),
                    html.Span(_verdict_label(sensor_assessment), className="rule-verdict-text"),
                ],
            ),
        ]
    )


def _sensor_category(sensor_assessment) -> str:
    if sensor_assessment.confirmed_flood:
        return "confirmed_flood"
    if sensor_assessment.possible_fault:
        return "possible_fault"
    return "normal"


_CATEGORY_LABELS = {"confirmed_flood": "Confirmed flood", "possible_fault": "Possible fault", "normal": "Normal"}


def _log_dot_style(category: str, stage: str) -> dict:
    """The leading type-icon for a log entry, drawn from the SAME _dot_style
    the map pins, sensor tabs and legend use — so "amber dot" means the same
    thing wherever it appears, and a possible_fault carries its dashed ring
    here exactly as it does on the map. Confirmed floods take the severity
    color of the stage that fired; a return to normal is always the Normal
    green regardless of what it fell from."""
    if category == "possible_fault":
        return _dot_style(SEVERITY_COLORS.get(stage, NEUTRAL_PIN_COLOR), FAULT_STROKE_COLOR, dashed=True, size="7px")
    if category == "confirmed_flood":
        return _dot_style(SEVERITY_COLORS.get(stage, NEUTRAL_PIN_COLOR), size="7px")
    return _dot_style(SEVERITY_COLORS["Normal"], size="7px")


def _update_event_log(assessment, previous_categories: dict, log_entries: list) -> tuple[dict, list]:
    """Edge-triggered per CLAUDE.md: append a log line only when a sensor's
    category (confirmed_flood/possible_fault/normal) actually changes from
    the previous tick, never on every tick. An empty `previous_categories`
    means no history yet (page load) — seed it silently rather than logging
    every sensor's initial state as a fake "transition".

    Entries are stored as dicts, not preformatted strings: the renderer needs
    the category and stage separately to pick the entry's dot color, and the
    store is internal to this app so its shape is ours to choose. Only what
    the entry displays is kept — when, which sensor, what it became. The
    previous state is dropped (the line above it in the log already says
    what it was) and so is the water_level value, which restated a number
    the stat tiles and charts were already showing.
    """
    current_categories = {sid: _sensor_category(sa) for sid, sa in assessment.sensors.items()}
    is_first_run = not previous_categories
    new_entries = list(log_entries)

    if not is_first_run:
        for sensor_id, category in current_categories.items():
            previous = previous_categories.get(sensor_id)
            if previous is not None and previous != category:
                sa = assessment.sensors[sensor_id]
                new_entries.append(
                    {
                        "time": sa.latest_timestamp.strftime("%b %d · %H:%M"),
                        "sensor": sensor_id,
                        "category": category,
                        "stage": sa.threshold_state,
                    }
                )

    return current_categories, new_entries[-MAX_LOG_ENTRIES:]


def _render_event_log(log_entries: list) -> html.Div:
    if not log_entries:
        return html.Div("No events yet.", className="log-empty")
    return html.Div(
        className="log-list",
        children=[
            html.Div(
                className="log-entry",
                children=[
                    html.Span(className="log-dot", style=_log_dot_style(entry["category"], entry["stage"])),
                    html.Span(entry["time"], className="log-time"),
                    html.Span(entry["sensor"], className="log-sensor"),
                    html.Span(_CATEGORY_LABELS[entry["category"]], className="log-state"),
                ],
            )
            for entry in reversed(log_entries)
        ],
    )


def _render_active_events(active_events: list, sim_step: int) -> html.Div:
    """The injector panel's status readout. Steps-remaining counts down as
    sim_step advances since this re-renders every tick; once it hits zero
    the event has finished unfolding but is still retained (its spike stays
    in the chart history), so the readout says so rather than sitting on a
    misleading "0 steps left" forever."""
    if not active_events:
        return html.Div("Status: no simulated events active", className="injector-status")
    rows = []
    for event in active_events:
        label = event.scenario
        if event.target_sensor:
            label += f" @ {event.target_sensor}"
        remaining = steps_remaining(event, sim_step)
        state = f"{remaining} steps left" if remaining else "complete — retained until Reset"
        rows.append(html.Div(f"{label} — magnitude {event.magnitude:g}, {state}", className="injector-status"))
    return html.Div(rows)


def _render_top_bar_injector_slot(active_events: list) -> str:
    if not active_events:
        return ""
    return f"{len(active_events)} injected event(s)"


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
    Output("last-assessed-step-store", "data"),
    Output("injector-active-events-display", "children"),
    Output("top-bar-injector-slot", "children"),
    Input("sim-step-store", "data"),
    Input("selected-sensor-store", "data"),
    Input("active-events-store", "data"),
    Input("reset-token-store", "data"),
    State("event-log-store", "data"),
    State("sensor-status-store", "data"),
    State("last-assessed-step-store", "data"),
)
def update_risk_fanout(
    sim_step, selected_sensor, active_events_raw, _reset_token, log_entries, previous_categories, last_assessed_step
):
    """Runs risk_assessment.assess_risk once per tick and fans its single
    result out to every display that depends on it — the map, top bar,
    readings/rule-eval panels, the sensor tabs' status dots, and the event
    log all read fields off the same `assessment`; none of them recompute
    risk.

    Selection is fanned out from here too (the tabs' `className` and the
    map's selection halo), because `selected-sensor-store` is already an
    Input: the tab highlight and the map ring are therefore written in the
    same response, and can't disagree even for a frame.

    Reads the injected frame through the shared memo rather than a Store: a
    full DataFrame isn't worth round-tripping through JSON every tick, and
    injected_readings is pure, so this and update_charts get the identical
    frame for the tick without either of them owning it.

    EVERY timestep is assessed, not just the one each tick lands on. Speed
    is steps-per-tick (see TICK_MS), so at 8x the clock jumps 8 steps
    between renders — and assessing only the landed step meant the rules
    could sample straight over a short event. A convective storm's 6-step
    envelope did exactly that at 8x: the chart drew the full spike (extendData
    appends every skipped row) while the pins never escalated and nothing
    logged. Sweeping the gap makes the LOGIC speed-independent — escalations,
    edge-triggered logging and rate-of-rise now see an identical sequence of
    timesteps at 1x and 8x. Rendering stays at tick rate: the landed step's
    assessment is the one every display reads.

    This is affordable because the expensive part of a tick was never
    assess_risk (10.8ms at step 100, 16.4ms at step 862) but building the
    frame it runs on. Each intermediate step reuses the frame already built
    for the landed step, sliced with searchsorted (0.05ms) instead of
    rebuilt (up to 9.5ms) — so the worst-case 8-step sweep costs ~132ms of
    the 1000ms budget rather than ~207ms.

    Reset clears the event log here. It has to be this callback — Dash
    allows one callback per Output and this one owns event-log-store — and
    it has to key off reset-token-store rather than "active_events is now
    empty", which is indistinguishable from page load. Clearing
    sensor-status-store alongside it is what stops the next tick logging a
    phantom transition out of the state the cleared entries described:
    _update_event_log treats an empty previous-categories dict as "no
    history yet" and re-seeds silently.
    """
    active_events = events_from_store(active_events_raw)
    injected = injected_readings(sim_step, active_events)

    reset_fired = any(t["prop_id"].startswith("reset-token-store") for t in ctx.triggered)
    categories = {} if reset_fired else (previous_categories or {})
    entries = [] if reset_fired else list(log_entries)

    # Steps the clock passed through since the last assessment, exclusive of
    # sim_step (assessed below as the landed step). Empty when nothing moved
    # — a selection change or a re-fire on the same step re-renders without
    # re-walking history — and empty on a loop wraparound, where sim_step is
    # BEHIND last_assessed_step and the replay is starting over rather than
    # continuing. The gap is bounded by the largest steps-per-tick (8),
    # since sim_step accumulates from a Store and so can't run ahead when a
    # response is dropped.
    previous_step = -1 if reset_fired else (last_assessed_step if last_assessed_step is not None else -1)
    timestamps = injected["timestamp"]
    for step in range(previous_step + 1, sim_step):
        # Slicing the frame already built for sim_step, NOT rebuilding it:
        # rows are sorted by timestamp, so this is the same frame
        # visible_readings would return for `step`.
        upto = timestamps.searchsorted(TIMELINE[step], side="right")
        categories, entries = _update_event_log(assess_risk(injected.iloc[:upto], SENSORS_META), categories, entries)

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

    # The landed step closes the sweep, so its transitions are logged the
    # same way an intermediate step's are.
    new_categories, new_log_entries = _update_event_log(assessment, categories, entries)
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
        sim_step,
        active_events_display,
        top_bar_injector_slot,
    )


if __name__ == "__main__":
    # 8050 (Dash's default) collides with an unrelated project's leftover
    # server on this machine — a dedicated port avoids that fight entirely.
    app.run(debug=True, port=8060)
