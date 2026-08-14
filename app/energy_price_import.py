"""Import jednoho měsíčního akčního ceníku z Excelu nebo CSV."""

from calendar import monthrange
from datetime import date, datetime
from io import BytesIO
from uuid import uuid4

import pandas as pd

from app.energy_calculator import EnergyCalculationError


TEMPLATE_COLUMNS = [
    "Komodita", "Produkt", "Sazba/pásmo", "Složka",
    "Cena platí od", "Cena platí do", "Cena Kč/MWh",
    "Stálý plat Kč/měsíc",
]


class EnergyPriceImportError(EnergyCalculationError):
    pass


def template_csv():
    example = pd.DataFrame([
        ["Elektřina", "Optimal 36", "Všechny", "VT", "2026-08-01", "2026-12-31", 2355, 127],
        ["Elektřina", "Optimal 36", "Všechny", "NT", "2026-08-01", "2026-12-31", 2355, 127],
        ["Plyn", "Optimal 36", "nad 7,56 do 63 MWh", "Jednotná", "2026-08-01", "2026-12-31", 825.5, 130],
    ], columns=TEMPLATE_COLUMNS)
    return example.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")


def _read(uploaded_file, file_name):
    raw = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file.read()
    try:
        if file_name.lower().endswith(".csv"):
            frame = pd.read_csv(BytesIO(raw), sep=None, engine="python", decimal=",")
        else:
            frame = pd.read_excel(BytesIO(raw), dtype=object)
    except Exception as exc:
        raise EnergyPriceImportError("Soubor se nepodařilo přečíst jako Excel nebo CSV.") from exc
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = set(TEMPLATE_COLUMNS) - set(frame.columns)
    if missing:
        raise EnergyPriceImportError(
            "V ceníku chybí sloupce: " + ", ".join(sorted(missing))
        )
    return frame[TEMPLATE_COLUMNS].dropna(how="all")


def _date(value, required=True):
    if value is None or pd.isna(value) or str(value).strip() == "":
        if required:
            raise EnergyPriceImportError("V ceníku chybí povinné datum.")
        return None
    parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        raise EnergyPriceImportError(f"Neplatné datum v ceníku: {value}")
    return parsed.date()


def _number(value, label):
    try:
        result = float(str(value).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise EnergyPriceImportError(f"Neplatná hodnota „{label}“: {value}") from exc
    if result < 0:
        raise EnergyPriceImportError(f"Hodnota „{label}“ nesmí být záporná.")
    return result


def import_monthly_price_list(conn, uploaded_file, file_name, action_month, username):
    frame = _read(uploaded_file, file_name)
    if frame.empty:
        raise EnergyPriceImportError("Nahraný ceník neobsahuje žádné řádky.")
    month_start = action_month.replace(day=1)
    month_end = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
    import_id = str(uuid4())
    prepared = []
    allowed_commodities = {"Elektřina", "Plyn"}
    allowed_components = {"Jednotná", "VT", "NT"}
    for number, row in frame.iterrows():
        commodity = str(row["Komodita"]).strip()
        product = str(row["Produkt"]).strip()
        rate = str(row["Sazba/pásmo"]).strip()
        component = str(row["Složka"]).strip()
        if commodity not in allowed_commodities:
            raise EnergyPriceImportError(f"Řádek {number + 2}: neplatná komodita „{commodity}“.")
        if component not in allowed_components:
            raise EnergyPriceImportError(f"Řádek {number + 2}: neplatná složka „{component}“.")
        if not product or not rate:
            raise EnergyPriceImportError(f"Řádek {number + 2}: chybí produkt nebo sazba/pásmo.")
        valid_from = _date(row["Cena platí od"])
        valid_to = _date(row["Cena platí do"], required=False)
        if valid_to and valid_to < valid_from:
            raise EnergyPriceImportError(f"Řádek {number + 2}: konec ceny je před začátkem.")
        prepared.append({
            "commodity": commodity, "product": product, "rate": rate,
            "component": component, "valid_from": valid_from, "valid_to": valid_to,
            "unit_price": _number(row["Cena Kč/MWh"], "Cena Kč/MWh"),
            "monthly_fee": _number(row["Stálý plat Kč/měsíc"], "Stálý plat Kč/měsíc"),
        })

    conn.execute("BEGIN TRANSACTION")
    try:
        list_ids = {}
        for item in prepared:
            key = (item["product"], item["commodity"])
            if key in list_ids:
                continue
            product_row = conn.execute("""
                SELECT id FROM energy_products
                WHERE lower(supplier)='innogy' AND lower(name)=lower(?) AND commodity=?
            """, [item["product"], item["commodity"]]).fetchone()
            if product_row:
                product_id = product_row[0]
            else:
                product_id = str(uuid4())
                conn.execute("""
                    INSERT INTO energy_products
                        (id,supplier,name,commodity,fixation_months)
                    VALUES (?,'innogy',?,?,36)
                """, [product_id, item["product"], item["commodity"]])
            conn.execute("UPDATE energy_price_lists SET active=FALSE WHERE product_id=?", [product_id])
            list_id = str(uuid4())
            list_name = f"{item['product']} – akce {month_start:%m/%Y}"
            conn.execute("""
                INSERT INTO energy_price_lists (
                    id,product_id,name,signing_valid_from,signing_valid_to,
                    active,note,import_id
                ) VALUES (?,?,?,?,?,TRUE,?,?)
            """, [list_id, product_id, list_name, month_start, month_end,
                   f"Nahráno ze souboru {file_name}", import_id])
            list_ids[key] = list_id

        for item in prepared:
            conn.execute("""
                INSERT INTO energy_price_periods (
                    id,price_list_id,rate_band,component,valid_from,valid_to,
                    unit_price,monthly_fee
                ) VALUES (?,?,?,?,?,?,?,?)
            """, [str(uuid4()), list_ids[(item["product"], item["commodity"])],
                   item["rate"], item["component"], item["valid_from"],
                   item["valid_to"], item["unit_price"], item["monthly_fee"]])
        conn.execute("""
            INSERT INTO energy_price_imports
                (id,file_name,action_month,imported_by,row_count,list_count)
            VALUES (?,?,?,?,?,?)
        """, [import_id, file_name, month_start, username, len(prepared), len(list_ids)])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"rows": len(prepared), "lists": len(list_ids), "month": month_start}
