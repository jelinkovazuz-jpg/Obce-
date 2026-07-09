import os
import requests
import duckdb
from dotenv import load_dotenv

# Načtení API klíče
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

# Připojení k databázi
DB = duckdb.connect("data/obce.duckdb")

# Načtení všech obcí
obce = DB.sql("""
SELECT kod_obce, nazev
FROM obce
ORDER BY nazev
""").fetchall()

# Hlavičky pro Google Places API
HEADERS = {
    "Content-Type": "application/json",
    "X-Goog-Api-Key": API_KEY,
    "X-Goog-FieldMask": "places.websiteUri"
}

# Projde všechny obce
for kod, obec in obce:

    print(f"Hledám: {obec}")

    body = {
        "textQuery": f"Obecní úřad {obec} Česká republika"
    }

    response = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers=HEADERS,
        json=body
    )

    if response.status_code != 200:
        print(f"❌ Chyba Google API ({response.status_code})")
        continue

    data = response.json()

    if "places" not in data or len(data["places"]) == 0:
        print("⚠️ Nenalezeno")
        continue

    web = data["places"][0].get("websiteUri")

    if not web:
        print("⚠️ Obec nemá uvedený web")
        continue

    # Uložení do databáze
    DB.execute("""
        UPDATE obce
        SET web = ?
        WHERE kod_obce = ?
    """, [web, kod])

    print(f"✅ Uloženo: {web}")

# Zavření databáze
DB.close()

print("\n🎉 Hotovo! Všechny nalezené weby byly uloženy do databáze.")