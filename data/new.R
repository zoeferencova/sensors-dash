library(CoSMoS)
library(ggplot2)
library(data.table)
library(jsonlite)

setwd("C:/Users/Office/Desktop/Zoe/sensors-dash/data")

n <- 864  # 3 days * 24h * 12 (5-min intervals)

# ============================================================================
# STEP 1: Generate ONE shared catchment rainfall series
# ============================================================================
rainfall <- generateTS(
  n = n,
  margdist = "ggamma",
  margarg = list(
    scale = 1,
    shape1 = 0.8,
    shape2 = 0.8
  ),
  p = 30,
  p0 = 0.7,
  acsvalue = acs(
    id = "paretoII",
    t = 0:30,
    scale = 1,
    shape = 0.75
  )
)

rain_shared <- rainfall[[1]]
rain_shared <- pmin(pmax(rain_shared, 0), 60)

# ============================================================================
# STEP 2: Inject a deliberate storm event (rainfall spike)
# ============================================================================
# Storm centered around timestep 300 (2.5 days in), ~10 hours duration
storm_center <- 300
storm_width <- 120  # ~10 hours at 5-min intervals
storm_magnitude <- 40  # mm/h spike

storm_spike <- storm_magnitude * exp(-((1:n - storm_center)^2) / (2 * (storm_width / 4)^2))
rain_shared <- rain_shared + storm_spike
rain_shared <- pmin(pmax(rain_shared, 0), 60)

# ============================================================================
# STEP 3: Define sensor lags (from the document table)
# ============================================================================
# Travel time lags (in timesteps of 5 minutes):
# S01 -> S02: 9 timesteps
# S01 -> S03: 12 timesteps (cumulative)
# S01 -> S04: 15 timesteps (cumulative)

lags <- c(
  S01 = 0,
  S02 = 9,
  S03 = 12,
  S04 = 15
)

# ============================================================================
# STEP 4: Create lagged rainfall series for each sensor
# ============================================================================
# Function to apply lag (shift forward in time)
apply_lag <- function(series, lag_steps) {
  if (lag_steps == 0) return(series)
  return(c(rep(0, lag_steps), series[1:(length(series) - lag_steps)]))
}

# Create lagged rainfall for each sensor with small per-sensor variation
rain_S01 <- rain_shared + rnorm(n, mean = 0, sd = 0.5)
rain_S01 <- pmin(pmax(rain_S01, 0), 60)

rain_S02 <- apply_lag(rain_shared, lags["S02"]) * 0.95 + rnorm(n, mean = 0, sd = 0.5)
rain_S02 <- pmin(pmax(rain_S02, 0), 60)

rain_S03 <- apply_lag(rain_shared, lags["S03"]) * 0.97 + rnorm(n, mean = 0, sd = 0.5)
rain_S03 <- pmin(pmax(rain_S03, 0), 60)

rain_S04 <- apply_lag(rain_shared, lags["S04"]) * 0.93 + rnorm(n, mean = 0, sd = 0.5)
rain_S04 <- pmin(pmax(rain_S04, 0), 60)

# ============================================================================
# STEP 5: Derive water level from shared rainfall
# ============================================================================
# Water level responds to rainfall with decay
# Baseline ~50 cm, normal up to ~100 cm, floods up to 220–300 cm
# Range: 40–300 cm (realistic Botič stage)

water_base <- 50  # baseline in cm
water_response_factor <- 2.5  # how much rainfall increases water level

# Generate base water level autocorrelated series
water_level_gen <- generateTS(
  n = n,
  margdist = "ggamma",
  margarg = list(
    scale = 1,
    shape1 = 1,
    shape2 = 1
  ),
  p = 30,
  acsvalue = acs(
    id = "paretoII",
    t = 0:30,
    scale = 1,
    shape = 0.9
  )
)

water_base_series <- water_level_gen[[1]]
water_base_series <- scale(water_base_series)[, 1]  # standardize

# Function to compute water level from rainfall (runoff + decay)
compute_water_level <- function(rainfall_series, base_water = 50, response_factor = 2.5) {
  water <- rep(base_water, length(rainfall_series))
  
  for (i in 2:length(rainfall_series)) {
    # Carry over 95% of previous level, add rainfall contribution
    water[i] <- water[i - 1] * 0.95 + base_water * 0.05 + (rainfall_series[i] * response_factor)
  }
  
  return(water)
}

# Compute water level for each sensor from its lagged rainfall
water_S01 <- compute_water_level(rain_S01, water_base, water_response_factor)
water_S02 <- compute_water_level(rain_S02, water_base, water_response_factor)
water_S03 <- compute_water_level(rain_S03, water_base, water_response_factor)
water_S04 <- compute_water_level(rain_S04, water_base, water_response_factor)

# Clip to realistic range (40–300 cm)
water_S01 <- pmin(pmax(water_S01, 40), 300)
water_S02 <- pmin(pmax(water_S02, 40), 300)
water_S03 <- pmin(pmax(water_S03, 40), 300)
water_S04 <- pmin(pmax(water_S04, 40), 300)

# ============================================================================
# STEP 6: Create catchment soil moisture (single series)
# ============================================================================
# Soil moisture derives from the shared rainfall, responds slowly
# High values persist (long memory), quick to rain, slow to dry

soil_moisture_gen <- generateTS(
  n = n,
  margdist = "beta",
  margarg = list(
    shape1 = 2,
    shape2 = 5
  ),
  p = 30,
  distbounds = c(0, 1),
  acsvalue = acs(
    id = "paretoII",
    t = 0:30,
    scale = 1,
    shape = 0.95
  )
)

soil_base <- soil_moisture_gen[[1]]
soil_base <- (soil_base - min(soil_base)) / (max(soil_base) - min(soil_base))

# Compute soil moisture with slow decay + rainfall influence
soil_catchment <- rep(0.4, n)  # start at 40% saturation

for (i in 2:n) {
  # Carry over 98% of previous value (very slow decay), add 2% rain effect
  soil_catchment[i] <- soil_catchment[i - 1] * 0.98 + 0.02 * (rain_shared[i] / 60)
}

soil_catchment <- pmin(pmax(soil_catchment, 0), 1)
soil_catchment <- soil_catchment * 100  # convert to 0–100 %

# ============================================================================
# STEP 7: Create timestamps (fixed: UTC offset)
# ============================================================================
timestamp <- seq(
  from = as.POSIXct("2024-12-31 23:00:00", tz = "UTC"),
  by = "5 min",
  length.out = n
)

timestamp_iso <- format(timestamp, "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")

# ============================================================================
# STEP 8: Build data frames for each sensor
# ============================================================================

# S01
S01_df <- data.frame(
  sensor_id = "S01",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S01, water_S01),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

# S02
S02_df <- data.frame(
  sensor_id = "S02",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S02, water_S02),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

# S03
S03_df <- data.frame(
  sensor_id = "S03",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S03, water_S03),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

# S04
S04_df <- data.frame(
  sensor_id = "S04",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S04, water_S04),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

# CATCHMENT (soil moisture only)
CATCHMENT_df <- data.frame(
  sensor_id = "CATCHMENT",
  timestamp = timestamp_iso,
  variable = "soil_moisture",
  value = soil_catchment,
  unit = "%",
  stringsAsFactors = FALSE
)

# ============================================================================
# STEP 9: Combine all data
# ============================================================================

all_data <- rbindlist(list(S01_df, S02_df, S03_df, S04_df, CATCHMENT_df))

# ============================================================================
# STEP 10: Verify spatial correlation (should be ~0.5+ between neighbours)
# ============================================================================

# Extract rainfall for each sensor at matching timestamps
rain_matrix <- data.frame(
  S01 = rain_S01,
  S02 = rain_S02,
  S03 = rain_S03,
  S04 = rain_S04
)

cat("\n=== Rainfall Cross-Sensor Correlations ===\n")
print(cor(rain_matrix))

# ============================================================================
# STEP 11: Write outputs
# ============================================================================

write_json(
  all_data,
  "sensor_data.json",
  pretty = TRUE,
  auto_unbox = TRUE
)

cat("\n=== Summary Statistics ===\n")
print(summary(all_data))

cat("\nFile written: sensor_data.json\n")
cat("Total rows:", nrow(all_data), "\n")
cat("Sensors: S01, S02, S03, S04, CATCHMENT\n")
cat("Variables per channel sensor: rainfall_intensity (mm/h), water_level (cm)\n")
cat("Variables for CATCHMENT: soil_moisture (%)\n")