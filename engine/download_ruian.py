import requests

url = "https://vdp.cuzk.gov.cz/vymenny_format/soucasna/ST_UKSG.xml.zip"

print("Stahuji seznam obcí...")

response = requests.get(url, timeout=60)

if response.status_code == 200:
    with open("ruian.zip", "wb") as f:
        f.write(response.content)
    print("✅ Hotovo! Soubor uložen jako ruian.zip")
else:
    print("Chyba:", response.status_code)