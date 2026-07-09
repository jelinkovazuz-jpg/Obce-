from geopy.geocoders import Nominatim

geolocator = Nominatim(user_agent="obce_app")

location = geolocator.geocode("Heřmanův Městec, Česká republika")

print(location)
print(location.latitude)
print(location.longitude)