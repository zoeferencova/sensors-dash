library(CoSMoS)
library(ggplot2)
library(data.table)
library(jsonlite)
library(here)

setwd(here())

n <- 864  # 3 days * 24h * 12 (5-min intervals)


# I Generated one shared rainfall series for the whole catchment.
# This represents the actual storm system before it reaches any individual
# sensor

rainfall <- generateTS(
  
  n = n,  
  # Number of time steps to generate.
  margdist = "ggamma",  
  # margdist - the statistical shape rainfall values are going to follow.
  #   ggamma- gamma distribution (mostly low values -> good for modelling rain)
  
  margarg = list(
    scale = 1,     # Overall scale of the distribution
    shape1 = 0.8,  # controls skewness
    shape2 = 0.8   # controls tail behaviour
    # Together shape1 and shape2 give a strongly right-skewed shape
  ),
  
  p = 30,  
  #controls how accurately the correlation pattern below gets reproduced.
  
  p0 = 0.7,  
  # p0 = 0.7 means ~70% of the time there is NO rain at all
  
  acsvalue = acs(
    id = "paretoII",
    # Controls how rainfall at one moment relates to rainfall later.
    # "paretoII" makes rainfall change gradually, creating clusters of rain events.
    t = 0:30,  
    # used to define the correlation
    
    scale = 1,     # Scale parameter of the Pareto II
    shape = 0.75   # Shape parameter - controls how fast correlation
    # weakens as the lag increases
  )
)

# generateTS() returns a list; the first element [[1]] is the actual
# numeric vector of generated rainfall values.
rain_shared <- rainfall[[1]]

# Keeps rainfall between 0 and 60 mm/h.
rain_shared <- pmin(pmax(rain_shared, 0), 60)

#                        ------------                      

# The background rainfall series is realistic but random 
# So we manually add storm, data will be shaped
# like a smooth bell curve. 

storm_center <- 300
# Which timestep the storm peaks at.
# Step 300 x 5 min/step = 1500 min = 25 hours = ~2.5 days into the simulation.

storm_width <- 120
# Roughly how many timesteps wide the storm event is.
# 120 steps x 5 min = 600 min = ~10 hours total storm duration.

storm_magnitude <- 21.5
# The extra rainfall intensity (mm/h) added at the very peak of the storm.

storm_spike <- storm_magnitude * exp(-((1:n - storm_center)^2) / (2 * (storm_width / 4)^2))
# This builds a smooth "bell curve" 
# (1:n - storm_center)  - how far each timestep is from the storm peak
# squaring -  makes this distance always positive
# dividing by 2*(width/4)^2 - controls how "spread out" the bump is
# exp(-...)- converts distance into a smooth curve that
# = 1 at the center and fades to 0 further away
# - multiplying by storm_magnitude - scales the peak height to 21.5 mm/h
# Result: storm_spike is ~0 for most of the timeline, and rises smoothly
# to a peak of 21.5 mm/h around timestep 300, tapering off over ~10 hours.

rain_shared <- rain_shared + storm_spike
# Add the storm bump on top of the existing background rainfall.

rain_shared <- pmin(pmax(rain_shared, 0), 60)

# Define sensor lags
lags <- c(
  S01 = 0,
  S02 = 9,
  S03 = 12,
  S04 = 15
)

#            ------ ------- -----
# Sensors further downstream don't see the storm at the exact same moment
# as the source. This function shifts a rainfall series forward in time
apply_lag <- function(series, lag_steps) {
  if (lag_steps == 0) return(series)
  return(c(rep(0, lag_steps),
           # Create a block of zeros, one for each step of delay
           series[1:(length(series) - lag_steps)]))
            #the final vector should stay the same length
}

#no lag, just small random sensor noice
rain_S01 <- rain_shared + rnorm(n, mean = 0, sd = 0.5)
                          #rnorm generates random numbers with normal distribution  
rain_S01 <- pmin(pmax(rain_S01, 0), 60)

#shift by its lag with 85% intensity + sensor noise
rain_S02 <- apply_lag(rain_shared, lags["S02"]) * 0.85 + rnorm(n, mean = 0, sd = 0.5)
rain_S02 <- pmin(pmax(rain_S02, 0), 60)

#shift by its lag with 70% intensity + sensor noise
rain_S03 <- apply_lag(rain_shared, lags["S03"]) * 0.70 + rnorm(n, mean = 0, sd = 0.5)
rain_S03 <- pmin(pmax(rain_S03, 0), 60)

#shift by its lag with 55% intensity + sensor noise
rain_S04 <- apply_lag(rain_shared, lags["S04"]) * 0.55 + rnorm(n, mean = 0, sd = 0.5)
rain_S04 <- pmin(pmax(rain_S04, 0), 60)

water_base <- 50            #baseline in cm
water_response_factor <- 1.2  #tuned down from 2.5

# Generating a synthetic baseline water-level time series.
water_level_gen <- generateTS(
  n = n,
  margdist = "ggamma",
  margarg = list(
    scale = 1,
    shape1 = 1,
    shape2 = 1
  ), p = 30,
  acsvalue = acs(
    id = "paretoII",
    t = 0:30,
    scale = 1,
    shape = 0.9
  )
)

water_base_series <- water_level_gen[[1]]
water_base_series <- scale(water_base_series)[, 1]

# Compute water level with faster 0.90 carryover decay
compute_water_level <- function(rainfall_series, base_water = 50, response_factor = 1.2) {
  water <- rep(base_water, length(rainfall_series))
  
  for (i in 2:length(rainfall_series)) {
    # 90% of previous level and add rainfall
    water[i] <- water[i - 1] * 0.90 + base_water * 0.10 + (rainfall_series[i] * response_factor)
  }
  
  return(water)
}

water_S01 <- compute_water_level(rain_S01, water_base, water_response_factor)
water_S02 <- compute_water_level(rain_S02, water_base, water_response_factor)
water_S03 <- compute_water_level(rain_S03, water_base, water_response_factor)
water_S04 <- compute_water_level(rain_S04, water_base, water_response_factor)

# upper limit raised to 350 so S01 reaches ~308
water_S01 <- pmin(pmax(water_S01, 40), 350)
water_S02 <- pmin(pmax(water_S02, 40), 350)
water_S03 <- pmin(pmax(water_S03, 40), 350)
water_S04 <- pmin(pmax(water_S04, 40), 350)

# Generate soil moisture data
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
# Rescale soil_base so it fits exactly [0, 1] using min-max normalization 
soil_base <- (soil_base - min(soil_base)) / (max(soil_base) - min(soil_base))

soil_catchment <- rep(0.4, n)
#start all values at 0.4, then loop will overwrite it

for (i in 2:n) {
  soil_catchment[i] <- soil_catchment[i - 1] * 0.98 + 0.02 * (rain_shared[i] / 60)
  # New moisture = 98% of previous value + small boost from current rainfall
}

soil_catchment <- pmin(pmax(soil_catchment, 0), 1)
soil_catchment <- soil_catchment * 100
# convert fraction to percentage for output units

timestamp <- seq(
  from = as.POSIXct("2024-12-31 23:00:00", tz = "UTC"),
  # start date: 2024-12-31 23:00:00
  by = "5 min",
  #step size = 5 min
  length.out = n
  #generate n timestamps total
  
)

timestamp_iso <- format(timestamp, "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")
# Convert each timestamp into a standard ISO 8601 string format

#         ----- Build data frames for sensors ----
S01_df <- data.frame(
  sensor_id = "S01",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S01, water_S01),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

S02_df <- data.frame(
  sensor_id = "S02",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S02, water_S02),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

S03_df <- data.frame(
  sensor_id = "S03",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S03, water_S03),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

S04_df <- data.frame(
  sensor_id = "S04",
  timestamp = timestamp_iso,
  variable = c(rep("rainfall_intensity", n), rep("water_level", n)),
  value = c(rain_S04, water_S04),
  unit = c(rep("mm/h", n), rep("cm", n)),
  stringsAsFactors = FALSE
)

# CATCHMENT_df is structured differently since it only has ONE variable
CATCHMENT_df <- data.frame(
  sensor_id = "CATCHMENT",
  timestamp = timestamp_iso,
  variable = "soil_moisture",
  value = soil_catchment,
  unit = "%",
  stringsAsFactors = FALSE
)

# Combine all data
all_data <- rbindlist(list(S01_df, S02_df, S03_df, S04_df, CATCHMENT_df))

rain_matrix <- data.frame(
  S01 = rain_S01,
  S02 = rain_S02,
  S03 = rain_S03,
  S04 = rain_S04
)

cat("\n=== Rainfall Cross-Sensor Correlations ===\n")
print(cor(rain_matrix))
# Compute the correlation matrix between all sensor rainfall columns

# write json file
write_json(
  all_data,
  "sensor_data.json",
  pretty = TRUE,
  auto_unbox = TRUE
)

# Print summary
cat("\n=== Peak Water Levels ===\n")
cat("S01 max:", max(water_S01), "\n")
cat("S02 max:", max(water_S02), "\n")
cat("S03 max:", max(water_S03), "\n")
cat("S04 max:", max(water_S04), "\n")

cat("\n=== Summary Statistics ===\n")
print(summary(all_data))

cat("\nFile written: sensor_data.json\n")
cat("Total rows:", nrow(all_data), "\n")
cat("Sensors: S01, S02, S03, S04, CATCHMENT\n")

