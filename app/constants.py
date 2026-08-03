"""Single source of truth for flood-stage thresholds and the severity color scale.

Every component that shows risk (top bar, charts, map, legend) imports from
here so the palette and threshold values can never drift apart between them.
"""

# ČHMÚ Praha-Nusle flood-stage limits for water_level (cm).
THRESHOLDS = {
    "Watch": 120,
    "Alert": 160,
    "Danger": 220,
    "Extreme": 306,
}

# Ordered from least to most severe.
STAGES = ["Normal", "Watch", "Alert", "Danger", "Extreme"]

SEVERITY_COLORS = {
    "Normal":  "#7A9B76",   # muted sage green — calm, natural
    "Watch":   "#C9B458",   # muted ochre/gold — mild caution
    "Alert":   "#C68A4E",   # muted terracotta/amber — clear step up
    "Danger":  "#B4553F",   # muted brick red — alarming but not neon
    "Extreme": "#7D3A32",   # deep muted maroon — gravest
}

# Pin color before assess_risk has produced a per-sensor state.
NEUTRAL_PIN_COLOR = "#3186cc"


def stage_for_water_level(value: float) -> str:
    """Map a water_level reading (cm) to its ČHMÚ risk stage. Pure lookup —
    this is Layer 1 of the alert logic in risk_assessment.py."""
    if value >= THRESHOLDS["Extreme"]:
        return "Extreme"
    if value >= THRESHOLDS["Danger"]:
        return "Danger"
    if value >= THRESHOLDS["Alert"]:
        return "Alert"
    if value >= THRESHOLDS["Watch"]:
        return "Watch"
    return "Normal"
