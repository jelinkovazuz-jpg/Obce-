# CRM obcí ČR

Streamlit aplikace pro vyhledávání českých obcí podle vzdálenosti a vedení
obchodních vztahů s obcemi.

## Funkce

- vyhledávání obcí v zadaném okruhu a export do Excelu,
- CRM pipeline a priority,
- přiřazení obce obchodníkovi,
- historie telefonátů, e-mailů, schůzek a poznámek,
- úkoly, termíny a upozornění na úkoly po termínu,
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
