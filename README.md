# Botič Flood Monitoring Dashboard

A flood early-warning dashboard for the Botič stream in Prague, built on
synthetic sensor data. Four virtual gauges (S01–S04) run downstream from
below Záběhlický rybník to the Vltava confluence; pre-generated time series
are replayed through the dashboard on a timer so the system reads as live.

The graded core is the alert logic: a single `assess_risk` function maps each
sensor's water level to the real ČHMÚ Praha–Nusle flood stages, checks
rate-of-rise, and confirms a downstream flood only when there was meaningful
upstream rainfall within the flood wave's travel time. A high reading with no
hydrological cause is flagged as a possible sensor fault instead of escalating
— false-alarm prevention rather than a bare threshold alarm.

## Running it

You need **Python 3.10 or newer**. Nothing else.

```bash
python run.py
```

On macOS and Linux, use `python3 run.py`.

That is the whole setup. On first run it creates a virtual environment,
installs the dependencies into it, and opens the dashboard in your browser.
Later runs reuse that environment and start immediately. If port 8060 is
already taken it quietly picks another one. Press `Ctrl+C` to stop.

## Using it

- **Play/Pause and 1x–8x** in the top bar drive the replay clock. Speed is
  timeline steps per second; the display refreshes once per second at every
  speed, and every timestep is evaluated regardless of speed.
- **The sensor tabs** (S01–S04) select which gauge the charts and readings
  show. Clicking a map pin does the same thing.
- **The event injector** (right panel) overlays synthetic storms and faults
  onto the readings the rules see. It never sets the risk state directly —
  the unmodified rules decide the outcome, which is what makes the demo
  honest. Injected events stay on the charts until you press Reset.
- **Rule evaluation** (left panel, below the charts) shows which conditions
  currently hold for the selected sensor and the verdict they produce.

Try the **Sensor fault** scenario: water level jumps with no rainfall
anywhere, and the system refuses to escalate it, flagging a data-quality
warning instead.

## Layout

```
run.py              one-command launcher
requirements.txt    direct dependencies
app/                all application code
  main.py           Dash app, layout and callbacks
  risk_assessment.py  the alert / nowcasting logic
  event_injector.py   scenario overlays
  charts.py           Plotly figure builders
  sensor_map.py       dash-leaflet map
  replay.py           replay clock
  data_loader.py      load and reshape readings
  constants.py        ČHMÚ thresholds and severity palette
data/               synthetic sensor data and sensor metadata
```

## Notes

Built with Dash rather than Streamlit: Streamlit's rerun model remounts the
charts and map on every tick, which makes a live-updating dashboard flicker.
Dash's callback model updates components in place — chart points are appended
with Plotly's `extendTraces` and only the map's marker layer is ever
redrawn, so tiles, pan and zoom never reset.
