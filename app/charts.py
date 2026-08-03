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


def _injected_points(series: pd.DataFrame) -> pd.DataFrame:
    if "injected" not in series.columns or not series["injected"].any():
        return series.iloc[0:0]
    return series[series["injected"]]


def build_water_level_figure(sensor_id: str) -> go.Figure:
    """Structure only — no data yet. Call update_water_level_figure right
    after to populate it, and on every tick thereafter."""
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
    )
    return fig


def update_water_level_figure(fig: go.Figure, series: pd.DataFrame) -> None:
    """Mutates `fig` in place: new x/y on the existing traces, nothing
    about the figure's structure touched."""
    fig.data[0].x = series["timestamp"]
    fig.data[0].y = series["value"]
    points = _injected_points(series)
    fig.data[1].x = points["timestamp"]
    fig.data[1].y = points["value"]


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
    )
    return fig


def update_rainfall_figure(fig: go.Figure, series: pd.DataFrame) -> None:
    fig.data[0].x = series["timestamp"]
    fig.data[0].y = series["value"]
    points = _injected_points(series)
    fig.data[1].x = points["timestamp"]
    fig.data[1].y = points["value"]
