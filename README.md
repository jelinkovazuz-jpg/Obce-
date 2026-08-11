# CRM obcí ČR

Streamlit aplikace pro vyhledávání českých obcí podle vzdálenosti a vedení
obchodních vztahů s obcemi.

## Funkce

- vyhledávání obcí v zadaném okruhu a export do Excelu,
- CRM pipeline a priority,
- přiřazení obce obchodníkovi,
- historie telefonátů, e-mailů, schůzek a poznámek,
- úkoly, termíny a upozornění na úkoly po termínu,
- individuální rozesílání nabídek vybraným obcím přes SMTP,
- ukládání kopií odeslaných nabídek do složky Odeslané přes IMAP,
- automatická evidence odpovědí a filtr obcí bez odpovědi po 7 dnech,
- automatická synchronizace e-mailové komunikace každých 10 minut,
- přihlášení uživatelů a role.

## Spuštění

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app/main.py
```

CRM tabulky se při prvním spuštění vytvoří automaticky v `data/obce.duckdb`.
Existující katalog obcí se nemění.

## Synchronizace Seznam Email

Zkopírujte hodnoty z `.env.example` do lokálního souboru `.env` a doplňte heslo
pro poštovní aplikaci. Aplikace používá zabezpečené IMAP spojení, synchronizuje
přijatou i odeslanou komunikaci a páruje ji s obcemi podle e-mailové adresy.
Ukládá text zpráv a názvy příloh, nikoliv samotné soubory příloh.
