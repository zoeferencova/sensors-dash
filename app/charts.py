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
# Injected stretches are marked by shading the BACKGROUND behind them rather
# than by restyling the data itself. The readings stay in their ordinary
# solid style throughout, which is the honest thing to draw: an injected
# reading is a real reading of a simulated world, and recolouring the line
# made the trace itself look like a different kind of measurement. Shading
# the region says "this window is simulated" while leaving the data to be
# read exactly as it is everywhere else.
#
# A greyish lavender, and NOT the warm orange this started as: every
# SEVERITY_COLORS entry is somewhere on the ochre->maroon warm ramp (Watch
# #C9B458, Alert #C68A4E, Danger #B4553F, Extreme #7D3A32), so an orange
# marker sat right on top of the Alert threshold line's hue while meaning
# something completely unrelated — "simulated", not "this severity". This
# hue is off that ramp entirely and equally clear of the two data blues.
#
# Desaturated rather than a saturated violet, to sit with the rest of the
# dashboard's muted palette; the trade is that a greyer fill reads fainter
# at the same alpha, so this carries slightly more than a vivid one would
# need. Still low enough that it stays BEHIND the data and the threshold
# lines and never competes with either.
INJECTED_BAND_FILL = "rgba(143, 136, 168, 0.18)"

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
            # No left pad: the graph div already starts at the panel's own
            # content margin, so any pad here indents the title relative to
            # every heading, stat tile and legend row in the sidebar.
            pad=dict(l=0),
            font=dict(size=13, color=TITLE_COLOR, weight=600),
        ),
        font=dict(family=FONT_FAMILY, size=11, color=AXIS_COLOR),
        plot_bgcolor="white",
        paper_bgcolor="white",
        # r=0 puts the plotted area's right edge on the panel's own content
        # margin; the old r=12 sat on top of automargin's reservation, so the
        # charts read as narrower than the stat tiles and legend rows stacked
        # around them.
        #
        # l is a FIXED floor rather than 0-plus-automargin, because the two
        # charts are stacked and share one time axis. Sized independently,
        # automargin reserves whatever each chart's own y labels need — 22px
        # for water level's "300" against 17px for rainfall's "2" — which
        # left the same timestamp sitting 5px apart in the two plots, so
        # their gridlines disagreed and a rainfall spike didn't line up with
        # the water-level rise it caused. A shared floor wide enough for both
        # pins them to the same x geometry. automargin still overrides it if
        # a label ever needs more, so this can't clip.
        margin=dict(l=28, r=0, t=44, b=5),
        # rainfall's injected bar trace needs to draw directly on top of the
        # real bar at the same x, not offset beside it — Plotly's default
        # "group" barmode would put same-x bars from two traces side by
        # side, halving each one's width instead of one replacing the
        # other's colour. Harmless on the water-level figure, which has no
        # bar traces at all.
        barmode="overlay",
        # No Plotly legend on either chart. "Simulated event" was the last
        # entry either one had, and it was drawn TWICE — once under the water
        # level chart and again under rainfall — for what is a single idea
        # spanning both. It now lives once, in the HTML legend below the
        # water-level chart (main.py's chart-threshold-legend), alongside the
        # threshold key it belongs with. Dropping the Plotly legend also
        # reclaims the vertical band it reserved under each plot.
        showlegend=False,
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
    # Time of day only. Left to itself a Plotly date axis prints a two-line
    # tick — the time, with the date stacked underneath at each day boundary
    # — which costs a whole extra row of chrome under BOTH charts to repeat
    # something the top bar's clock is already showing continuously. An
    # explicit tickformat replaces that hierarchy with a single line.
    #
    # X only, and deliberately not folded into axis_style above: that dict
    # goes to both axes, and a date format on the y axis would wreck the
    # rainfall chart's ".1f" (see build_rainfall_figure).
    fig.update_xaxes(tickformat="%H:%M")
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


def _add_injection_bands(fig: go.Figure, bands) -> None:
    """Shade the time ranges under injection.

    layer="below" puts the band beneath the traces AND beneath the threshold
    lines, so it never dulls a stage colour or the data it sits behind. It is
    a layout shape, which is precisely why it can span steps the replay has
    not reached: shapes are not part of the extendData append path, so this
    is drawn once when the figure is rebuilt (on trigger/reset/sensor change)
    and the sliding x-window reveals it as the clock advances. See
    event_injector.injected_spans for the full argument.
    """
    for start, end in bands:
        fig.add_vrect(
            x0=start,
            x1=end,
            fillcolor=INJECTED_BAND_FILL,
            line_width=0,
            layer="below",
        )


def build_water_level_figure(sensor_id: str, x_range=None, bands=()) -> go.Figure:
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

    _add_injection_bands(fig, bands)

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


def build_rainfall_figure(sensor_id: str, x_range=None, bands=()) -> go.Figure:
    """See build_water_level_figure for the `x_range` / template rationale."""
    fig = go.Figure()
    fig.add_trace(
        go.Bar(x=[], y=[], name="rainfall_intensity", marker_color=RAINFALL_COLOR, showlegend=False)
    )

    _add_injection_bands(fig, bands)

    fig.update_layout(**_base_layout(f"{sensor_id} — Rainfall in mm/h"))
    _apply_axis_style(fig)
    # One decimal place on every tick, so the labels are a consistent width.
    # Y tick labels right-align against the axis, which means a bare "2" ends
    # up 14px further right than the water-level chart's "300" directly above
    # it — the two label columns read as misaligned even though both plot
    # areas start at exactly the same x. Padding "2" to "2.0" fills that
    # gutter. It also drops the ragged mixed precision the default produced
    # ("0", "0.5", "1", "1.5"), and matches the data, which is continuous
    # mm/h to several decimals rather than whole numbers.
    fig.update_yaxes(tickformat=".1f")
    _apply_fixed_x_range(fig, x_range)
    return fig


def update_rainfall_figure(fig: go.Figure, series: pd.DataFrame) -> None:
    """See update_water_level_figure's docstring: plain lists, not
    pandas/numpy arrays, so the client-side trace stays extendTraces-safe."""
    fig.data[0].x = series["timestamp"].tolist()
    fig.data[0].y = series["value"].tolist()
