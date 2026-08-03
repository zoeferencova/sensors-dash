library(CoSMoS)
library(ggplot2)
library(data.table)
library(jsonlite)

setwd("C:/Users/revaz/OneDrive/Desktop/zoe&ano")

n <- 864  # 3 days * 24h * 12 (5-min intervals)

#                               RAINFALL (0–60 mm/h)
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

rain <- rainfall[[1]]
rain <- pmin(pmax(rain, 0), 60)

water_level <- generateTS(
  n = n,
  margdist = "ggamma",
  margarg = list(
    scale = 5,
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

water <- water_level[[1]]
water <- water + 0.15 * rain

water <- scale(water)
water <- (water - min(water)) / (max(water) - min(water))
water <- water * 4
water <- pmin(pmax(water, 0), 4)

soil_moisture <- generateTS(
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

soil <- soil_moisture[[1]]

# rainfall increases soil moisture slightly
soil <- soil + 0.01 * rain
soil <- pmin(pmax(soil, 0), 1)
soil <- soil * 100

timestamp <- seq(
  from = as.POSIXct("2025-01-01 00:00:00", tz = "UTC"),
  by = "5 min",
  length.out = n
)

timestamp <- format(timestamp, "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")

simulation <- data.frame(
  timestamp = timestamp_iso,
  rainfall_intensity = rain,
  water_level = water,
  soil_moisture = soil
)

summary(simulation)

dt <- as.data.table(simulation)

long_dt <- melt(
  dt,
  id.vars = "timestamp",
  variable.name = "variable",
  value.name = "value"
)

long_dt[, unit := fifelse(
  variable == "rainfall_intensity", "mm/h",
  fifelse(
    variable == "water_level", "m",
    "%"
  )
)]

long_dt[, sensor_id := "S01"]

long_dt <- long_dt[, .(
  sensor_id,
  timestamp,
  variable,
  value,
  unit
)]

# write.csv(
#   long_dt,
#   "raw_simulation_S01.csv",
#   row.names = FALSE
# )

S01 <- read.csv(file = "raw_simulation_S01.csv")
S02 <- read.csv(file = "raw_simulation_S02.csv")
S03 <- read.csv(file = "raw_simulation_S03.csv")
S04 <- read.csv(file = "raw_simulation_S04.csv")

S02$sensor_id <- "S02"
S03$sensor_id <- "S03"
S04$sensor_id <- "S04"

all_data <- rbindlist(list(S01, S02, S03, S04))

# write.csv(
#   all_data,
#   "all_data.csv",
#   row.names = FALSE
# )

dta <- read.csv(file = "all_data.csv")

# write_json(
#   all_data,
#   "sensor_data.json",
#   pretty = TRUE,
#   auto_unbox = TRUE
# )

