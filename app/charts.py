"""Plotly chart builders for the sensor detail panel.

Each chart is built ONCE per sensor (`build_*`) and cached by the caller
(main.py); every later tick calls the matching `update_*` to mutate that
SAME Figure's trace data in place, rather than constructing a fresh Figure.
Keeping the object stable — same trace count/order, same layout, threshold
lines and legend never rebuilt — means Streamlit only ever ships new data
arrays to the frontend, letting Plotly's own client-side update path patch
the existing chart instead of tearing it down and reinitializing everything
(layout, shapes, annotations) on every replay step.
"""

import pandas as pd
import plotly.graph_objects as go

from constants import SEVERITY_COLORS, THRESHOLDS

# Trace colors are drawn from the dashboard's own steel-blue accent family
# (the same --steel that styles the selected tab, the primary buttons and
# the map's selection ring) rather than the stock plotly blues they started
# as, so a chart reads as part of the panel it sits in. Both stay clear of
# SEVERITY_COLORS: a trace's color must never look like a risk state, since
# the threshold lines crossing it are exactly that.
WATER_LEVEL_COLOR = "#3d5a73"
RAINFALL_COLOR = "#8ba4b8"
# The one deliberate exception: injected readings must read as "this is not
# real data", so they get an accent nothing else on the page uses.
#
# Violet specifically, and NOT the warm orange this used to be: every
# SEVERITY_COLORS entry is somewhere on the ochre->maroon warm ramp (Watch
# #C9B458, Alert #C68A4E, Danger #B4553F, Extreme #7D3A32), so an orange
# overlay sat right on top of the Alert threshold line's hue while meaning
# something completely unrelated — "simulated", not "this severity". Violet
# is off that ramp entirely and equally clear of the two data blues, so it
# can't be misread as a risk state at any threshold.
INJECTED_COLOR = "#7d5ba6"
# Dashed, so the overlay is distinguishable from the solid threshold lines
# by KIND and not only by colour — a broken line reads as "synthetic" even
# before the colour registers, and survives a greyscale print of the writeup.
INJECTED_LINE_DASH = "dash"

# --- Visual language ------------------------------------------------------
# Modelled on Google Flood Hub's discharge panel (the layout reference in
# CLAUDE.md): quiet chrome, data forward. No axis lines or box, no tick
# marks, horizontal+vertical gridlines in a very light grey, units carried
# by a small top-left title instead of rotated axis titles, and the
# threshold values moved off the plot entirely into an HTML legend
# (main.py's chart_threshold_legend, below the water-level chart) rather
# than a Plotly legend — hlines are layout shapes, so they never carried a
# legend entry natively, and the fake zero-data proxy traces that used to
# stand in for them are gone.
#
# The font stack mirrors assets/style.css. Inter is already loaded there
# for the HTML, but Plotly draws its labels as SVG text with its own font
# settings, so the charts opt in separately or they fall back to Plotly's
# default sans-serif and look foreign next to the rest of the dashboard.
FONT_FAMILY = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
TITLE_COLOR = "#2c3e50"   # same ink as .clock-time
AXIS_COLOR = "#6b7681"    # same muted grey as .btn-secondary text
GRID_COLOR = "#e9ecef"    # same light grey as the .speed-segmented track


def _base_layout(title: str) -> dict:
    """Layout chrome shared by both charts, so they read as one system.

    Built once per figure and never touched again — a tick only appends
    trace data (extendData) and slides the x-axis range, so none of this
    is re-sent or recomputed during playback.
    """
    return dict(
        title=dict(
            text=title,
            # Flush top-left of the whole graph, like the reference's
            # "Discharge in m³/s" — small and quiet, not a headline.
            # xref="container" (not "paper") puts it left of the y tick
            # labels rather than above the plot area.
            x=0,
            xref="container",
            xanchor="left",
            pad=dict(l=6),
            font=dict(size=13, color=TITLE_COLOR, weight=600),
        ),
        font=dict(family=FONT_FAMILY, size=11, color=AXIS_COLOR),
        plot_bgcolor="white",
        paper_bgcolor="white",
        # Left/bottom stay small because automargin grows them to fit the
        # tick labels; the top leaves room for the title.
        margin=dict(l=8, r=12, t=44, b=5),
        # rainfall's injected bar trace needs to draw directly on top of the
        # real bar at the same x, not offset beside it — Plotly's default
        # "group" barmode would put same-x bars from two traces side by
        # side, halving each one's width instead of one replacing the
        # other's colour. Harmless on the water-level figure, which has no
        # bar traces at all.
        barmode="overlay",
        # Explicit rather than left to Plotly's default: with the threshold
        # proxy traces gone, "simulated event" is the only showlegend=True
        # trace left, and Plotly.js hides the legend entirely when fewer
        # than two traces are legend-eligible unless told otherwise — which
        # would silently drop the one label that explains the injected
        # line/bar colour's meaning.
        showlegend=True,
        legend=dict(
            orientation="h",
            x=0,
            xanchor="left",
            y=-0.16,
            yanchor="top",
            # A step smaller than the tick labels: the threshold legend is a
            # key you consult, not a reading, and it sat level with the data
            # labels while competing with them for attention.
            font=dict(size=10, color=AXIS_COLOR),
            # The legend here is a key, not a control: clicking an entry
            # would hide a threshold line or the injected-event markers,
            # which is never something the user wants mid-replay.
            itemclick=False,
            itemdoubleclick=False,
            bgcolor="rgba(0,0,0,0)",
        ),
        hoverlabel=dict(font=dict(family=FONT_FAMILY, size=11)),
        template=None,
    )


def _apply_axis_style(fig: go.Figure) -> None:
    """Strip the axis chrome down to gridlines and labels."""
    axis_style = dict(
        title_text=None,
        showgrid=True,
        gridcolor=GRID_COLOR,
        gridwidth=1,
        showline=False,
        zeroline=False,
        ticks="",
        automargin=True,
        tickfont=dict(size=11, color=AXIS_COLOR),
    )
    fig.update_xaxes(**axis_style)
    fig.update_yaxes(**axis_style)
    # Y ONLY, deliberately not folded into axis_style above: the x axis is a
    # date axis carrying an explicit pinned range (_apply_fixed_x_range), and
    # "include zero" on a date axis means the 1970 epoch — it would blow the
    # sliding window wide open.
    #
    # Both quantities are physically non-negative, so autorange padding below
    # zero is drawing an impossible region. It showed most on rainfall, which
    # is flat 0 for long stretches and empty on first paint: Plotly's default
    # empty-trace range is [-1, 1], so the chart opened with half its height
    # given over to negative millimetres of rain before the first bar landed.
    fig.update_yaxes(rangemode="tozero")


def _injected_line_trace() -> go.Scatter:
    """Always present (even with empty data) so the trace list — and thus
    the figure's structure — never changes shape between an uninjected and
    an injected tick; only in-place data updates are ever needed after the
    initial build.

    Draws the injected SEGMENT of the water-level line dashed in the
    injected violet, directly over the real line. This
    works with no change to the extendData contract at all: `apply_injections`
    already overlays the modified value into the same `value` column the
    real (trace 0) line reads, so trace 0's blue line already passes through
    the exact same y-values at every injected timestamp. This trace draws
    only the injected-flagged subset of points — update_water_level_figure
    was already computing that exact subset for the old diamond markers — as
    a LINE instead of markers, in orange, layered on top of (drawn after)
    trace 0. Where it has data, it visually replaces the blue segment
    beneath it; everywhere else, trace 0 shows through unchanged. Since it
    stops exactly where the `injected` flag stops, the boundary between real
    and synthetic is sharp — a visual-honesty requirement, not a bug to
    smooth over with interpolation.
    """
    return go.Scatter(
        x=[],
        y=[],
        mode="lines",
        name="simulated event",
        line=dict(color=INJECTED_COLOR, width=2.5, dash=INJECTED_LINE_DASH),
        showlegend=True,
    )


def _injected_bar_trace() -> go.Bar:
    """The rainfall-chart equivalent of _injected_line_trace: the injected
    subset of bars, redrawn in the injected violet directly over the real
    bars (via `barmode="overlay"` in _base_layout).

    Colour alone carries it here — a bar can't be dashed, and a hatch
    pattern was tried and dropped: at 5-minute resolution a 12-hour window
    holds ~144 bars only a couple of pixels wide, where any fill pattern
    turns to noise rather than reading as texture. The violet is doing the
    same job it does on the line, and the two charts sit directly above one
    another, so the association carries across without needing a second
    visual device.
    """
    return go.Bar(
        x=[],
        y=[],
        name="simulated event",
        marker_color=INJECTED_COLOR,
        showlegend=True,
    )


def _apply_fixed_x_range(fig: go.Figure, x_range) -> None:
    """Pin the x-axis to an explicit [start, end] window instead of letting
    it autoscale to whatever has been appended so far.

    Without this the axis rescales on every tick — the first few points span
    the full width, then compress as history accumulates, so the spacing
    between points keeps changing. An explicit range of constant width
    (Flood-Hub style) means the trace moves through an axis whose scale
    never changes under it.

    The caller passes the sliding window `[now - CHART_WINDOW, now]` (see
    main.chart_x_range), so this is only the *initial* range for a freshly
    built figure; every later tick slides that same axis forward with a
    client-side Plotly.relayout (main.slide_chart_x_range) rather than
    sending the figure again.

    This is layout-only, so it does NOT interfere with extendData: appends
    still mutate trace data in place, and because autorange is off, Plotly
    won't recompute the axis when they land.
    """
    if x_range is None:
        return
    start, end = x_range
    fig.update_xaxes(range=[start, end], autorange=False)


def injected_points(series: pd.DataFrame) -> pd.DataFrame:
    """The injected-flagged rows only. Used for the rainfall BARS, where
    each bar stands alone: a bar carries no line between it and its
    neighbour, so there is nothing to break and nothing to join. Crucially
    the bars must NOT pick up the boundary points injected_overlay adds for
    the line — a bar drawn in the simulated colour is a claim that THAT
    reading is simulated, so including the real bars either side would
    mislabel real data."""
    if "injected" not in series.columns or not series["injected"].any():
        return series.iloc[0:0]
    return series[series["injected"]]


def injected_overlay(series: pd.DataFrame, context=None) -> tuple[list, list]:
    """(x, y) for the injected LINE overlay: each contiguous run of injected
    readings, plus the real reading immediately either side of it, with runs
    separated by a None break.

    Three things this gets right that filtering to the injected rows did not:

    - **No diagonals across gaps.** Two separate injections (a second event,
      or the same scenario fired again later) landed in one trace as a single
      unbroken sequence, so Plotly drew a straight line from the end of the
      first spike to the start of the second — straight across untouched real
      data, implying a synthetic reading everywhere in between. `None` is
      Plotly's line-break sentinel, so each run becomes its own polyline.

    - **Clean joins at both edges.** The pulse envelope is already nonzero at
      the first flagged reading, so a run drawn from its own first injected
      row starts part-way up the rise, floating above the real line. Emitting
      the real reading immediately BEFORE the run (and immediately AFTER it)
      makes the overlay begin and end on points the blue line already passes
      through, so the coloured segment grows out of the real series instead
      of hovering beside it.

    - **Works incrementally.** `context` is the last already-rendered row. It
      is never emitted as data of its own — only as the leading boundary
      point of a run that opens on the first row of `series`. That keeps the
      whole computation lookBACK-only, so a per-tick extendData append
      produces exactly the points a full rebuild would, without needing to
      see a timestep the replay hasn't reached yet.
    """
    if "injected" not in series.columns:
        return [], []

    xs: list = []
    ys: list = []
    previous_injected = bool(context["injected"]) if context is not None else False
    previous_x = context["timestamp"] if context is not None else None
    previous_y = context["value"] if context is not None else None

    for timestamp, value, is_injected in zip(series["timestamp"], series["value"], series["injected"]):
        if is_injected and not previous_injected and previous_x is not None:
            # Opening a run: break away from any earlier run, then step back
            # onto the real line so the colour change starts at the baseline.
            xs.append(None)
            ys.append(None)
            xs.append(previous_x)
            ys.append(previous_y)
        # Inside a run, or the first real reading after one ends (the closing
        # boundary, which lands the overlay back on the real line).
        if is_injected or previous_injected:
            xs.append(timestamp)
            ys.append(value)
        previous_injected, previous_x, previous_y = bool(is_injected), timestamp, value

    return xs, ys


def build_water_level_figure(sensor_id: str, x_range=None) -> go.Figure:
    """Structure only — no data yet. Call update_water_level_figure right
    after to populate it, and on every tick thereafter.

    `x_range` pins the time axis to the current sliding window (see
    _apply_fixed_x_range) so the line moves through a stable axis instead
    of the axis rescaling under it as points append.

    template=None (below) drops plotly.py's embedded default theme from the
    serialized figure — several KB of colorscale/font/background JSON that
    Plotly.js already applies as its own client-side default when no
    template is given, so this is a payload-size cut with no visual change.
    Smaller payload matters here specifically because this full-figure
    rebuild only fires occasionally (sensor switch, event trigger) while
    tiny extendData responses fire every tick; a slow-to-transmit rebuild
    response can otherwise arrive after a newer/faster one and clobber it.
    """
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[],
            y=[],
            mode="lines",
            name="water_level",
            line=dict(color=WATER_LEVEL_COLOR, width=2),
            showlegend=False,
        )
    )
    fig.add_trace(_injected_line_trace())

    # Solid, unlabelled threshold lines (the reference draws them this way);
    # each one's name and value are carried by the HTML legend main.py
    # renders below this chart, not a Plotly legend proxy — hlines are
    # layout *shapes*, so they were never going to appear in a Plotly legend
    # on their own regardless.
    for stage, level in THRESHOLDS.items():
        fig.add_hline(y=level, line=dict(color=SEVERITY_COLORS[stage], width=1.5))

    fig.update_layout(**_base_layout(f"{sensor_id} — Water level in cm"))
    _apply_axis_style(fig)
    _apply_fixed_x_range(fig, x_range)
    return fig


def update_water_level_figure(fig: go.Figure, series: pd.DataFrame) -> None:
    """Mutates `fig` in place: new x/y on the existing traces, nothing
    about the figure's structure touched.

    Assigns plain Python lists, not pandas Series/numpy arrays directly:
    Plotly.py serializes a numeric numpy-backed array using a compact
    binary {"dtype", "bdata"} typed-array encoding rather than a plain JSON
    array. The Dash extendData path (main.py's update_charts) later calls
    Plotly.extendTraces on this same trace, which does `array.push(...)` on
    the client — that throws/no-ops on a typed array (fixed-size), silently
    breaking all future live updates. Plain lists decode to ordinary,
    resizable JS arrays on the client, which is what extendTraces requires.
    """
    fig.data[0].x = series["timestamp"].tolist()
    fig.data[0].y = series["value"].tolist()
    # No `context`: a rebuild redraws the whole visible history, so the first
    # row here really is the start of the series.
    overlay_x, overlay_y = injected_overlay(series)
    fig.data[1].x = overlay_x
    fig.data[1].y = overlay_y


def build_rainfall_figure(sensor_id: str, x_range=None) -> go.Figure:
    """See build_water_level_figure for the `x_range` / template rationale."""
    fig = go.Figure()
    fig.add_trace(
        go.Bar(x=[], y=[], name="rainfall_intensity", marker_color=RAINFALL_COLOR, showlegend=False)
    )
    fig.add_trace(_injected_bar_trace())

    fig.update_layout(**_base_layout(f"{sensor_id} — Rainfall in mm/h"))
    _apply_axis_style(fig)
    _apply_fixed_x_range(fig, x_range)
    return fig


def update_rainfall_figure(fig: go.Figure, series: pd.DataFrame) -> None:
    """See update_water_level_figure's docstring: plain lists, not
    pandas/numpy arrays, so the client-side trace stays extendTraces-safe."""
    fig.data[0].x = series["timestamp"].tolist()
    fig.data[0].y = series["value"].tolist()
    points = injected_points(series)
    fig.data[1].x = points["timestamp"].tolist()
    fig.data[1].y = points["value"].tolist()
