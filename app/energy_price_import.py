"""Import měsíčních akčních ceníků Innogy Optimal 36."""

from calendar import monthrange
from datetime import date
from io import BytesIO
from pathlib import Path
import re
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


MONEY_PATTERN = re.compile(r"\d{1,3}(?:[ \u00a0]\d{3})*,\d{2}")
DATE_PATTERN = re.compile(r"\d{1,2}\.\s*\d{1,2}\.\s*\d{4}")
BUNDLED_PRICE_LIST_DIR = (
    Path(__file__).resolve().parent.parent / "data" / "default_price_lists"
)


def ensure_bundled_price_lists(conn, username="system", directory=None):
    """Restore repository-bundled action PDFs after an ephemeral Cloud reboot."""
    source_dir = Path(directory) if directory else BUNDLED_PRICE_LIST_DIR
    imported = []
    if not source_dir.exists():
        return imported
    for path in sorted(source_dir.glob("*.pdf")):
        exists = conn.execute(
            "SELECT 1 FROM energy_price_imports WHERE file_name=? LIMIT 1",
            [path.name],
        ).fetchone()
        if exists:
            continue
        parsed = parse_innogy_price_pdf(path.read_bytes(), path.name)
        import_parsed_pdf(conn, parsed, username)
        imported.append(path.name)
    return imported


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


def _pdf_date(value):
    return pd.to_datetime(re.sub(r"\s+", "", value), dayfirst=True).date()


def _money_values(line):
    return [_number(value, "cena v PDF") for value in MONEY_PATTERN.findall(line)]


def parse_innogy_price_pdf(pdf_bytes, file_name="cenik.pdf"):
    """Parse the Innogy Optimal 36 action-sheet layout supplied by the user."""
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(
                page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                for page in pdf.pages
            )
    except Exception as exc:
        raise EnergyPriceImportError(f"PDF {file_name} se nepodařilo přečíst.") from exc
    if not text.strip() or "Optimal 36" not in text:
        raise EnergyPriceImportError(
            f"PDF {file_name} není rozpoznaný ceník Innogy Optimal 36."
        )

    signing = re.search(
        r"smlouvy uzavřené od\s+(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\s+do\s+"
        r"(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})",
        text, flags=re.IGNORECASE,
    )
    if not signing:
        raise EnergyPriceImportError(f"V PDF {file_name} nebyla nalezena platnost akce.")
    signing_from, signing_to = map(_pdf_date, signing.groups())

    period_line = next(
        (line for line in text.splitlines() if "Celé období dodávky" in line), ""
    )
    dates = [_pdf_date(value) for value in DATE_PATTERN.findall(period_line)]
    if len(dates) != 8:
        raise EnergyPriceImportError(
            f"V PDF {file_name} nebylo rozpoznáno pět cenových období."
        )
    periods = [
        (signing_from, dates[0]), (dates[1], dates[2]),
        (dates[3], dates[4]), (dates[5], dates[6]), (dates[7], None),
    ]

    price_lines = [
        (line, _money_values(line)) for line in text.splitlines()
        if len(MONEY_PATTERN.findall(line)) >= 12
    ]
    rows = []
    if re.search(r"\belektřina\b", text, flags=re.IGNORECASE):
        commodity = "Elektřina"
        if len(price_lines) != 2:
            raise EnergyPriceImportError(
                f"V PDF {file_name} nebyly rozpoznány dvě skupiny sazeb elektřiny."
            )
        rate_groups = [
            ["Všechny"], ["D27d", "D56d", "D57d", "C27d", "C56d"],
        ]
        for (_, values), rates in zip(price_lines, rate_groups):
            values = values[-12:]
            fee, prices = values[0], values[2::2]
            if len(prices) != 5:
                raise EnergyPriceImportError(f"V PDF {file_name} chybí cena elektřiny.")
            for rate in rates:
                for component in ("VT", "NT"):
                    for (valid_from, valid_to), price in zip(periods, prices):
                        rows.append(_parsed_row(
                            commodity, "Optimal 36", rate, component,
                            valid_from, valid_to, price, fee,
                        ))
    elif re.search(r"\bplyn\b", text, flags=re.IGNORECASE):
        commodity = "Plyn"
        if len(price_lines) != 4:
            raise EnergyPriceImportError(
                f"V PDF {file_name} nebyla rozpoznána čtyři pásma spotřeby plynu."
            )
        bands = [
            ("do 1,89 MWh", r"^do\s+1,89\s+"),
            ("nad 1,89 do 7,56 MWh", r"^nad\s+1,89\s+do\s+7,56\s+"),
            ("nad 7,56 do 63 MWh", r"^nad\s+7,56\s+do\s+63\s+"),
            ("nad 63 MWh", r"^nad\s+63\s+"),
        ]
        for ((line, _), (band, label_pattern)) in zip(price_lines, bands):
            values = _money_values(re.sub(label_pattern, "", line, flags=re.IGNORECASE))
            fee, prices = values[0], values[2::2]
            if len(prices) != 5:
                raise EnergyPriceImportError(f"V PDF {file_name} chybí cena plynu.")
            for (valid_from, valid_to), price in zip(periods, prices):
                rows.append(_parsed_row(
                    commodity, "Optimal 36", band, "Jednotná",
                    valid_from, valid_to, price, fee,
                ))
    else:
        raise EnergyPriceImportError(f"V PDF {file_name} nebyla rozpoznána komodita.")
    return {
        "file_name": file_name, "commodity": commodity, "product": "Optimal 36",
        "signing_from": signing_from, "signing_to": signing_to, "rows": rows,
    }


def _parsed_row(commodity, product, rate, component, valid_from, valid_to,
                unit_price, monthly_fee):
    return {
        "commodity": commodity, "product": product, "rate": rate,
        "component": component, "valid_from": valid_from, "valid_to": valid_to,
        "unit_price": unit_price, "monthly_fee": monthly_fee,
    }


def import_parsed_pdf(conn, parsed, username):
    return _store_prepared(
        conn, parsed["rows"], parsed["file_name"], parsed["signing_from"],
        parsed["signing_to"], username,
    )


def import_monthly_price_list(conn, uploaded_file, file_name, action_month, username):
    frame = _read(uploaded_file, file_name)
    if frame.empty:
        raise EnergyPriceImportError("Nahraný ceník neobsahuje žádné řádky.")
    month_start = action_month.replace(day=1)
    month_end = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
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

    return _store_prepared(conn, prepared, file_name, month_start, month_end, username)


def _store_prepared(conn, prepared, file_name, signing_from, signing_to, username):
    import_id = str(uuid4())
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
            list_name = f"{item['product']} – akce {signing_from:%m/%Y}"
            conn.execute("""
                INSERT INTO energy_price_lists (
                    id,product_id,name,signing_valid_from,signing_valid_to,
                    active,note,import_id
                ) VALUES (?,?,?,?,?,TRUE,?,?)
            """, [list_id, product_id, list_name, signing_from, signing_to,
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
        """, [import_id, file_name, signing_from, username, len(prepared), len(list_ids)])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"rows": len(prepared), "lists": len(list_ids), "month": signing_from}
