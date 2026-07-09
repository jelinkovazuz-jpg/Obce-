import time
import duckdb
from geopy.geocoders import Nominatim

conn = duckdb.connect("data/obce.duckdb")

geolocator = Nominatim(user_agent="obce_app")

obce = conn.execute("""
SELECT kod_obce, nazev
FROM obce
WHERE latitude IS NULL
""").fetchall()

print(f"Načteno {len(obce)} obcí.")

for kod, nazev in obce:
    try:
        location = geolocator.geocode(f"{nazev}, Česká republika")

        if location:
            conn.execute("""
                UPDATE obce
                SET latitude = ?, longitude = ?
                WHERE kod_obce = ?
            """, (
                location.latitude,
                location.longitude,
                kod
            ))

            print(f"✔ {nazev}")

        else:
            print(f"✖ Nenalezeno: {nazev}")

        time.sleep(1)

    except Exception as e:
        print(f"Chyba u {nazev}: {e}")

conn.close()

print("Hotovo.")