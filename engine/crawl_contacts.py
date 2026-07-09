import re
import requests
import duckdb
from bs4 import BeautifulSoup
from urllib.parse import urljoin

DB = duckdb.connect("data/obce.duckdb")

obce = DB.execute("""
SELECT
    kod_obce,
    nazev,
    web
FROM obce
WHERE web IS NOT NULL
""").fetchall()

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

# stránky, které stojí za to projít
KEYWORDS = [
    "kontakt",
    "kontakty",
    "obecni-urad",
    "obecní-úřad",
    "urad",
    "úřad",
    "povinne",
    "povinné",
    "starosta"
]


def najdi_udaje(html):

    email = None
    telefon = None
    ico = None

    emails = re.findall(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        html
    )

    if emails:
        email = emails[0]

    telefony = re.findall(
        r"(?:\+420\s?)?(?:\d{3}\s?\d{3}\s?\d{3})",
        html
    )

    if telefony:
        telefon = telefony[0]

    match = re.search(
        r"IČO[:\s]*([0-9]{8})",
        html,
        re.IGNORECASE
    )

    if match:
        ico = match.group(1)

    return email, telefon, ico


for kod, obec, web in obce:

    print(f"\n=== {obec} ===")

    try:

        response = requests.get(
            web,
            headers=HEADERS,
            timeout=10
        )

        if response.status_code != 200:
            print("Nepodařilo se otevřít web.")
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        email, telefon, ico = najdi_udaje(response.text)

        # pokud něco chybí, projdi stránky Kontakt apod.
        if not (email and telefon and ico):

            odkazy = []

            for a in soup.find_all("a", href=True):

                href = a["href"].lower()

                if any(slovo in href for slovo in KEYWORDS):
                    odkazy.append(
                        urljoin(web, a["href"])
                    )

            for odkaz in set(odkazy):

                try:

                    r = requests.get(
                        odkaz,
                        headers=HEADERS,
                        timeout=10
                    )

                    e, t, i = najdi_udaje(r.text)

                    if not email and e:
                        email = e

                    if not telefon and t:
                        telefon = t

                    if not ico and i:
                        ico = i

                    if email and telefon and ico:
                        break

                except:
                    pass

        DB.execute("""
        UPDATE obce
        SET
            email=?,
            telefon=?,
            ico=?
        WHERE kod_obce=?
        """, [email, telefon, ico, kod])

        print("Email :", email)
        print("Telefon:", telefon)
        print("IČO    :", ico)

    except Exception as e:
        print(e)

DB.close()

print("\n🎉 Hotovo.")