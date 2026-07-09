import requests
from bs4 import BeautifulSoup

url = "https://mesta.obce.cz/zsu/vyhledat-14199.htm"

headers = {
    "User-Agent": "Mozilla/5.0"
}

r = requests.get(url, headers=headers, timeout=30)

print("Status:", r.status_code)
print("URL:", r.url)

with open("webhouse.html", "w", encoding="utf-8") as f:
    f.write(r.text)

print("HTML uloženo.")