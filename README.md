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
- import smluv z Innogy iSales Excelu a párování k obcím podle IČO,
- kalkulace nabídek elektřiny a plynu pro více odběrných míst,
- výpočet úspory za 12 a 36 měsíců přes více cenových období,
- podpora VT/NT, záporných úspor a odvození zahájení z výpovědní doby,
- administrace produktů, akčních ceníků a jejich časových cenových úseků,
- přihlášení uživatelů a role.

## Kalkulačka energií

Kalkulačka je v záložce **Kalkulace**. Nejprve založte nabídku zákazníka,
potom k ní přidávejte odběrná místa. Souhrn započítává všechna místa včetně
záporných výsledků. Výpočty zahrnují pouze obchodní část ceny a stálý obchodní
plat, vždy bez DPH.

V administraci jsou předvyplněné vzorové ceny Optimal 36 z roku 2026. Před
odesláním skutečné nabídky je ověřte proti platnému ceníku. Nové měsíční akce se
zakládají jako nový akční ceník; jednotlivé ceny lze průběžně přidávat a měnit.

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
