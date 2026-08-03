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

INJECTED_MARKER_COLOR = "#e07b00"


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


def injected_points(series: pd.DataFrame) -> pd.DataFrame:
    if "injected" not in series.columns or not series["injected"].any():
        return series.iloc[0:0]
    return series[series["injected"]]


def build_water_level_figure(sensor_id: str) -> go.Figure:
    """Structure only — no data yet. Call update_water_level_figure right
    after to populate it, and on every tick thereafter.

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
    fig.add_trace(go.Scatter(x=[], y=[], mode="lines", name="water_level", line=dict(color="#1f4e79"), showlegend=False))
    fig.add_trace(_injected_marker_trace())

    for stage, level in THRESHOLDS.items():
        fig.add_hline(
            y=level,
            line=dict(color=SEVERITY_COLORS[stage], dash="dash", width=1.5),
            annotation_text=f"{stage} ({level} cm)",
            annotation_position="right",
            annotation_font_color=SEVERITY_COLORS[stage],
        )

    fig.update_layout(
        title=f"{sensor_id} — water_level (cm)",
        xaxis_title="time",
        yaxis_title="water_level (cm)",
        margin=dict(t=40, r=80),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        template=None,
    )
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


def build_rainfall_figure(sensor_id: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Bar(x=[], y=[], name="rainfall_intensity", marker_color="#4c72b0", showlegend=False))
    fig.add_trace(_injected_marker_trace())

    fig.update_layout(
        title=f"{sensor_id} — rainfall_intensity (mm/h)",
        xaxis_title="time",
        yaxis_title="mm/h",
        margin=dict(t=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        template=None,
    )
    return fig


def update_rainfall_figure(fig: go.Figure, series: pd.DataFrame) -> None:
    """See update_water_level_figure's docstring: plain lists, not
    pandas/numpy arrays, so the client-side trace stays extendTraces-safe."""
    fig.data[0].x = series["timestamp"].tolist()
    fig.data[0].y = series["value"].tolist()
    points = injected_points(series)
    fig.data[1].x = points["timestamp"].tolist()
    fig.data[1].y = points["value"].tolist()
