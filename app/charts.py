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
# The one deliberate exception: injected points must read as "this is not
# real data", so they keep a warm accent that nothing else on the page uses.
INJECTED_MARKER_COLOR = "#e07b00"

# --- Visual language ------------------------------------------------------
# Modelled on Google Flood Hub's discharge panel (the layout reference in
# CLAUDE.md): quiet chrome, data forward. No axis lines or box, no tick
# marks, horizontal+vertical gridlines in a very light grey, units carried
# by a small top-left title instead of rotated axis titles, and the
# threshold values moved off the plot into the legend.
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
            font=dict(size=13, color=TITLE_COLOR),
        ),
        font=dict(family=FONT_FAMILY, size=11, color=AXIS_COLOR),
        plot_bgcolor="white",
        paper_bgcolor="white",
        # Left/bottom stay small because automargin grows them to fit the
        # tick labels; the top leaves room for the title.
        margin=dict(l=8, r=12, t=44, b=8),
        legend=dict(
            orientation="h",
            x=0,
            xanchor="left",
            y=-0.16,
            yanchor="top",
            font=dict(size=11, color=AXIS_COLOR),
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


def _threshold_legend_trace(stage: str, level: int) -> go.Scatter:
    """A legend-only proxy for a threshold line.

    The threshold itself is an hline, which is a layout *shape* and so can
    never appear in the legend. The reference puts the stage name over its
    numeric value beneath the chart, which is what the `<br>` gives; doing
    it this way also lets the lines themselves stay unlabelled, freeing the
    ~80px right margin the old inline annotations needed.

    Carries no data (`[None]`), so it draws nothing and cannot affect
    autorange. It sits at trace index 2+ — main line and injected markers
    keep indices 0 and 1, which is what main.update_charts extends.
    """
    return go.Scatter(
        x=[None],
        y=[None],
        mode="markers",
        marker=dict(color=SEVERITY_COLORS[stage], size=7),
        name=f"{stage}<br>{level}",
        showlegend=True,
        hoverinfo="skip",
    )


def _injected_marker_trace() -> go.Scatter:
    """Always present (even with empty data) so the trace list — and thus
    the figure's structure — never changes shape between an uninjected and
    an injected tick; only in-place data updates are ever needed after the
    initial build."""
    return go.Scatter(
        x=[],
        y=[],
        mode="markers",
        name="simulated event",
        marker=dict(color=INJECTED_MARKER_COLOR, size=8, symbol="diamond", line=dict(color="#7a4200", width=1)),
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
    if "injected" not in series.columns or not series["injected"].any():
        return series.iloc[0:0]
    return series[series["injected"]]


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
    fig.add_trace(_injected_marker_trace())

    # Solid, unlabelled threshold lines (the reference draws them this way);
    # each one's name and value are carried by its legend proxy below.
    for stage, level in THRESHOLDS.items():
        fig.add_hline(y=level, line=dict(color=SEVERITY_COLORS[stage], width=1.5))
    for stage, level in THRESHOLDS.items():
        fig.add_trace(_threshold_legend_trace(stage, level))

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
    points = injected_points(series)
    fig.data[1].x = points["timestamp"].tolist()
    fig.data[1].y = points["value"].tolist()


def build_rainfall_figure(sensor_id: str, x_range=None) -> go.Figure:
    """See build_water_level_figure for the `x_range` / template rationale."""
    fig = go.Figure()
    fig.add_trace(
        go.Bar(x=[], y=[], name="rainfall_intensity", marker_color=RAINFALL_COLOR, showlegend=False)
    )
    fig.add_trace(_injected_marker_trace())

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
