from dotenv import load_dotenv
import os
import requests

load_dotenv()

API_KEY = os.getenv("GOOGLE_API_KEY")

obec = "Heřmanův Městec obecní úřad"

url = "https://places.googleapis.com/v1/places:searchText"

headers = {
    "Content-Type": "application/json",
    "X-Goog-Api-Key": API_KEY,
    "X-Goog-FieldMask": "places.displayName,places.websiteUri"
}

data = {
    "textQuery": obec
}

response = requests.post(url, headers=headers, json=data)

print(response.status_code)
print(response.text)