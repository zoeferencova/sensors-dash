# Botič Flood Monitoring Dashboard — Project Brief

## What this is
An academic + portfolio project: a flood early-warning **dashboard** for the Botič stream in Prague, built on **synthetic sensor data**. This is a student project (Environmental Data Science BSc, ČZU). There is no real sensor hardware — "live" data is a frontend illusion created by replaying pre-generated synthetic time series through the dashboard on a timer.

Two-person split:
- **Me (frontend + logic):** the dashboard, the replay engine, the alert/nowcasting logic, the event injector, the sensor network design. **This is what this repo is.**
- **Ano (backend, separate):** generates the synthetic sensor data in R using CoSMoS. Her output is the input to this project.

## Tech stack
- **Dash** (Python) — the dashboard framework. Migrated from Streamlit to get true flicker-free live updates (Streamlit's rerun model forces chart/map remounts on every tick; Dash's callback model updates components surgically).
- **Plotly** — interactive time-series charts, updated live via the `extendData` pattern (Plotly.extendTraces — appends points without full-figure teardown; the figure prop is set once and never reassigned).
- **dash-leaflet** — the sensor map. Critical rule: `dl.Map` and `dl.TileLayer` are static and never appear as callback Outputs; only a `dl.LayerGroup`'s children get updated, so tiles/pan/zoom never reset.
- **pandas** — data handling.
- Replay/state via `dcc.Store` + `dcc.Interval` (Dash's equivalent of Streamlit session_state + timed rerun).
- Virtual environment at project root (`.venv`). Dependencies in `requirements.txt`.

### Migration note
Ported from a working Streamlit version. Framework-agnostic modules (`constants`, `data_loader`, `risk_assessment`, `charts`, and the event-construction logic in `event_injector`) carried over unchanged. Only UI wiring (`main`, `replay`, map construction, injector sidebar) was rewritten for Dash. The Streamlit→Dash switch and the reasons for it (source-level Streamlit rendering limitations) are worth noting in the writeup as evidence of tool-tradeoff judgment.

## Project structure
```
sensors-dash/          # Dash project (the original Streamlit version is kept separately as sensors-dashboard)
├── data/          # Ano's sensor_data.json, my sensors.json
├── app/           # all Python code (main app folder)
├── requirements.txt
├── .venv/
└── CLAUDE.md      # this file
```

## The data contract (input format)
Records are long-format: `sensor_id, timestamp, variable, value, unit`.
- 5-minute intervals.
- Variables: `rainfall_intensity` (mm/h), `water_level` (cm — see note), `soil_moisture` (%).
- Delivered as `data/sensor_data.json` (array of records).

**Important — the data is being revised by Ano.** Current file has known issues being fixed. Target final shape:
- **S01–S04**: four channel sensors, each reporting `rainfall_intensity` + `water_level`. These are spatially correlated — derived from one shared catchment rainfall series with per-sensor lags (see below).
- **CATCHMENT**: a reserved sensor_id reporting `soil_moisture` only — a single catchment-wide antecedent-wetness series (NOT per-sensor). Slow-decaying, derived from the shared rainfall.
- `water_level` should sit in realistic Botič stage values in **cm**: dry baseline ~40–50, normal up to ~100, flood events pushing past 120 (up to ~220–300 at peak).

Build defensively so the app works whether soil_moisture is per-sensor (old) or CATCHMENT-only (new).

## The sensor network (my design)
Four sensors on the regulated Botič reach below the last impoundment (Záběhlický rybník), running downstream to the Vltava confluence. Upstream → downstream:

| ID | Location | Lat | Lon | Role |
|----|----------|-----|-----|------|
| S01 | Below Záběhlický rybník outflow | 50.05453 | 14.48059 | Upstream boundary / lead-time |
| S02 | Vršovice (Sámova/Vršovická) | 50.06773 | 14.45010 | Residential assets at risk |
| S03 | Nusle (Folimanka/Ostrčilovo nám.) | 50.06653 | 14.43147 | Urban core; coincides w/ real ČHMÚ gauge |
| S04 | Vltava confluence (Výtoň/Vyšehrad) | 50.06703 | 14.41488 | Terminal gauge / confluence |

(Coordinates above are decimal approximations of the DMS values — verify/refine when building sensors.json.)

**Propagation lags** (storm reaches sensors in sequence, cumulative from S01, at 5-min steps):
- S01 → S02: ~9 timesteps
- S02 → S03: ~3 timesteps (≈12 from S01)
- S03 → S04: ~3 timesteps (≈15 from S01)

Soil moisture represents the **upstream contributing catchment** (natural permeable land above the reach where runoff is generated) — NOT the ground at any channel sensor. This is why it's a single CATCHMENT value, not per-sensor.

## Alert thresholds (calibrated to real data)
Risk states use the actual ČHMÚ Praha–Nusle flood-stage limits for the Botič, keyed on `water_level` in cm:
- **Normal:** < 120
- **Watch** (1st SPA / bdělost): ≥ 120
- **Alert** (2nd SPA / pohotovost): ≥ 160
- **Danger** (3rd SPA / ohrožení): ≥ 220
- **Extreme:** ≥ 306

## The alert / nowcasting logic (the headline deliverable)

This is the intellectually graded core. **Architecture principle:** one function computes the full risk assessment from the system state at the current moment, and returns both the risk states and which conditions fired. Everything else (dashboard, map colors, rule panel) reads from that one output. The injector feeds *inputs* to this function; it never sets the risk state directly. Keep this separation — it's what makes the system honest rather than theater.

Suggested shape: `assess_risk(readings_up_to_now, sensors_meta) -> RiskAssessment`, where the return carries per-sensor states, the overall catchment state, and a per-condition true/false breakdown for the rule-evaluation panel.

### Layer 1 — per-sensor threshold state
For each channel sensor (S01–S04), map its current `water_level` (cm) to a stage using the ČHMÚ bands above: Normal <120, Watch ≥120, Alert ≥160, Danger ≥220, Extreme ≥306. Pure lookup. This is the raw single-sensor read before any confirmation.

### Layer 2 — rate-of-rise
For each sensor, compute the rise in `water_level` over the last 3 timesteps (15 min). Flag `rising_fast` when the sustained rate exceeds **~0.5 cm/min** (i.e. ~7.5 cm over the 15-min window). Rationale to cite: the 2013 flood rose ~1 cm/min; the cutoff is set at roughly half that so dangerous rises are caught *before* reaching that extreme. `RATE_OF_RISE_CM_PER_MIN = 0.5` — a clearly-marked tunable constant.

### Layer 3 — multi-sensor confirmation / false-alarm prevention (the key rule)
A downstream sensor reading high water is only a **confirmed flood** if there was meaningful upstream rainfall within a lookback window that covers the propagation lag.

- Cumulative lags from CLAUDE.md: S02 is ~9 timesteps below S01; S03 ~12 below S01; S04 ~15 below S01.
- **Confirmation is against S01, the upstream boundary gauge**, at each downstream sensor's cumulative lag — NOT against each sensor's immediate neighbor. Rationale: rainfall is one shared catchment series observed with lag at each point, so S01 carries the earliest, cleanest instance of the upstream signal, and this matches the cumulative-lag constants directly without introducing an unstated neighbor-to-neighbor lag table. S01 is also closest to where runoff is generated, making it the best proxy for "did the catchment get rain that's now heading downstream."
- **Use a windowed lookback, not a single exact lag.** For a downstream sensor, look for S01 rainfall in a window spanning `[lag − MARGIN, lag + MARGIN]` timesteps earlier. Rationale to cite: flood-wave travel time varies with flow conditions (faster in high flow — exactly when floods happen), so a window centered on the lag avoids missing real events when water moves faster/slower than the point estimate. `LAG_MARGIN = 3` timesteps (±15 min), tunable. The window is **clipped at "now"** — never looks past the current replay moment (can't confirm against rain that hasn't happened yet).
- "Meaningful upstream rainfall" = S01 `rainfall_intensity` exceeded `RAINFALL_CONFIRM_MM_H` (tunable, ~2 mm/h) at any point in the window.
- **Confirmed flood:** high water (Watch+) at a downstream sensor AND S01 rainfall found in the window → the sensor's threshold state stands as a real flood signal.

### Layer 4 — fault detection (the impressive negative case)
This is the *negation* of Layer 3, not separate machinery. If a downstream sensor reads high water (Watch+) but there is **no** confirming S01 rainfall in the window → do NOT treat as a flood. Flag it as `possible_fault` / data-quality warning. This demonstrates false-alarm prevention: the system refuses to escalate a lone high reading with no hydrological cause.

**S01 (the boundary gauge) is a special case:** it has no upstream neighbour to cross-check against — it IS the upstream cause. So S01 self-confirms on `own_rain_confirmed OR rising_fast` (either signal alone is independent evidence of a real event). OR, not AND: requiring both would make the earliest-warning gauge the *hardest* to trigger, which defeats the purpose of an upstream sensor. Because the window is clipped at "now" and S01's lag is 0, S01's own-rainfall check naturally becomes "meaningful rain in the last 15 min" with no special-casing needed. (Watch during testing: if OR makes S01 fire spuriously often on the synthetic data, revisit.)

### Layer 5 — overall catchment risk state
The headline state for the top bar is the **maximum** confirmed severity across the four sensors (worst sensor drives it — standard practice for warning systems; you report worst-case). Confirmed floods escalate the overall state; unconfirmed high readings surface as a data-quality flag and do NOT escalate the headline state.

### Constants to expose (all tunable, defensible defaults)
```
RATE_OF_RISE_CM_PER_MIN = 0.5   # ~half the 2013 ~1 cm/min rate
LAG_MARGIN = 3                  # ±15 min around the propagation lag
RAINFALL_CONFIRM_MM_H = 2.0     # "meaningful" upstream rain for confirmation
# ČHMÚ stage bands (cm): 120 / 160 / 220 / 306
# cumulative lags (timesteps from S01): S02=9, S03=12, S04=15
```

### Data assumptions
This logic consumes clean, reshaped per-sensor series (via `get_series` or equivalent). It assumes no NA/invalid values — a separate load/clean layer (built when wiring this up) guarantees that upstream, so the logic itself doesn't re-check validity. Fault detection here is about physically-implausible-but-present readings, NOT missing data (which the clean layer handles).

### Rule-evaluation panel output
The assessment must expose a per-condition breakdown so the dashboard panel can show which conditions are currently true, e.g. per sensor: `water_level ≥ Watch ✓`, `rising_fast ✗`, `upstream_rain_confirmed ✓` → `CONFIRMED FLOOD` vs `✓ / ✗ / ✗` → `possible fault`. This legibility panel is high-value; the assessment function should return the raw booleans, not just the final state.

## Dashboard sections (target layout — Flood Hub-inspired)
Reference: Google Flood Hub. Three-zone layout with a top bar.

- **Top bar (global controls + headline state):** overall catchment risk state (with severity color), current simulated timestamp, and the replay controls (play/pause + speed). These are global — they act on the whole dashboard — so they live here, separate from the reactive content panels. The event injector controls also live here (or in a clearly-separated control area), since they're controls, not displays.
- **Left panel (fixed, non-collapsible) — selected-sensor detail + status overview:** top section is the selected sensor's time-series charts (Flood Hub discharge-panel style; water_level with ČHMÚ threshold reference lines 120/160/220/306 cm, plus rainfall) and current readings below them. Bottom section (visually separated) is an all-sensor status summary — the four sensors' current states at a glance. (Detail-up-top, overview-below, like Flood Hub's left panel.)
- **Center — map (dash-leaflet):** four sensor pins colored by individual status. Click a pin to select that sensor (drives the left panel). Static tiles; only marker layer updates.
- **Right panel (top) — event injector:** dedicated control section for firing scenarios. It's a *control*, so keep it visually distinct (border/header/background) from the display panels.
- **Right panel (bottom) — event/alert log:** running list of confirmed floods / faults as they occur. Edge-triggered (logs on status transitions, not every tick). Pure display.

Organizing principle: top bar = global controls; left = selected-sensor detail + status overview; right top = injector (control); right bottom = log (display). Controls stay visually distinct from read-outs.

## The replay engine
Static synthetic data is stepped through on a timer to read as "live." In Dash: a `dcc.Interval` fires on a timer, a `dcc.Store` holds the current timestep index, and a callback advances the index and exposes data up to the current step. Play/pause and speed controls drive the Interval. (This replaces the Streamlit session_state + timed-rerun approach from the original version.)

## The event injector (demo feature)
A control that injects extreme events into the **reading stream** (NOT directly into the alert state — the injector modifies inputs, the unmodified rules decide the output; keep this separation, it's what makes the demo honest). Overlay at read time rather than mutating stored data, so reset is trivial and injected values can be shown distinctly.

Planned scenarios:
- **Convective storm** — sharp rainfall spike at one upstream sensor; should trigger a watch then resolve.
- **Catchment-wide event** — rainfall across sensors with downstream lag; should escalate to full warning.
- **Sensor fault** — water level jumps with NO rainfall anywhere; should NOT trigger a flood alert, should flag a data-quality warning. (Demonstrates false-alarm prevention — the most impressive scenario.)
- **Saturated antecedent** — moderate rainfall onto already-high catchment soil moisture; should escalate faster than the same rain on dry ground.

## Build order
1. ✅ Environment setup (done).
2. Data layer: load sensor_data.json, reshape long→wide. **(current step)**
3. One time-series chart rendering (today's skeleton goal).
4. sensors.json + Folium map with pins.
5. Replay engine.
6. Dashboard core (readings panel, status).
7. Alert/nowcasting logic + rule-evaluation panel.
8. Event injector.
9. Integrate Ano's corrected data; verify alerts fire on her designed storm event.
10. Writeup: network design justification, README, portfolio framing.

Test the alert logic against a hand-edited copy of the JSON with a manual storm spike — don't wait on Ano's final data to build/test detection.

## Constraints / philosophy
- Keep it lean: three variables are enough; don't add decorative data.
- Every design choice should be defensible in an interview (this doubles as portfolio material).
- The reach is a clean, regulated, un-impounded channel (short culvert at Otakarova junction, but no impoundments between sensors) — this is what makes the simple travel-time lags valid. Don't introduce anything that assumes buffering/reservoir routing between sensors.
