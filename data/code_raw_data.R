library(CoSMoS)
library(ggplot2)
library(data.table)

setwd("C:/Users/revaz/OneDrive/Desktop/zoe&ano")
# Data is generated for 3 days in every 5 minutes
# 3 days * 24 hours * 12 measurements in every hour = 864

rainfall = generateTS(
  n = 864,
#marginal distribution of values, shape of data and behaviors
  margdist = "ggamma", #ggamma because rain is right skewed and positive
  margarg = list(
    scale = 1, #How strong rain is
    shape1 = 0.8,  #small values
    shape2 = 0.8   #extereme values
    # with shape1 and shape2 we get lots of small rainfall values as well as occasional 
    #   heavy rains, it has strong right skewed distribution
  ),
#tells us how strongly a value depends on previous values.
  p = 30, #correlation between today and 30 days ago.
  p0 = 0.7, #70% of days are forced to be zero rainfall
#target autocorrelation sequence using the acs() function
  acsvalue = acs(
  #Chooses the mathematical shape of the autocorrelation function (ACF)
    id = "paretoII", #decreases slowly and produces long memory.
    t = 0:30, #Computes autocorrelations for lags
    scale = 1, #Controls how quickly the correlation decreases. Higher value lower scale
    #how quickly the dependence between values disappears as the lag increases:
    shape = 0.75 # 0.75
  )
)

plot(rain_water)

water_level <- generateTS(
  n = 864,
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

soil_moisture <- generateTS(
  n = 864,
  margdist = "beta",
  margarg = list(
    shape1 = 2,
    shape2 = 5
  ),
  p = 30,
  distbounds = c(0,1),
  acsvalue = acs(
    id = "paretoII",
    t = 0:30,
    scale = 1,
    shape = 0.95
  )
)

timestamp <- seq(
  from = as.POSIXct("2025-01-01 00:00"),
  by = "5 min",
  length.out = 864
)

rain <- rainfall[[1]]
water <- water_level[[1]]
soil <- soil_moisture[[1]]

simulation <- data.frame(
  timestamp = timestamp,
  rainfall_intensity = rain,
  water_level = water,
  soil_moisture = soil
)

write.csv(
  simulation,
  "raw_simulation.csv",
  row.names = FALSE
)
















