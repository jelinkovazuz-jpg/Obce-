import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta

import pandas as pd


REQUIRED_COLUMNS = {"EAN/EIC", "Zákazník", "Datum narození/IČO", "Typ komodity"}
DATE_COLUMNS = {
    "Vygenerován Econ", "Podepsán Econ", "Datum podpisu",
    "Datum odeslání do evidence", "Datum ověření", "Datum scanu",
    "Datum doručení", "Datum nezpracování",
}


class InnogyImportError(RuntimeError):
    pass


def init_innogy(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS innogy_imports (
            id VARCHAR PRIMARY KEY, file_name VARCHAR, imported_by VARCHAR,
            imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            total_rows INTEGER, matched_rows INTEGER, unmatched_rows INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS innogy_contracts (
            contract_key VARCHAR PRIMARY KEY, import_id VARCHAR,
            kod_obce INTEGER, match_method VARCHAR, ico VARCHAR,
            customer_name VARCHAR, ean_eic VARCHAR, opportunity_status VARCHAR,
            ex_code VARCHAR, seller_name VARCHAR, category VARCHAR,
            commodity VARCHAR, product VARCHAR, current_supplier VARCHAR,
            generated_at TIMESTAMP, signed_at TIMESTAMP, verified_at TIMESTAMP,
            verification_status VARCHAR, consumption_gas DOUBLE,
            consumption_high DOUBLE, consumption_low DOUBLE,
            termination_by VARCHAR, termination_method VARCHAR,
            phone VARCHAR, note VARCHAR, raw_data VARCHAR,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _clean(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_name(value):
    text = unicodedata.normalize("NFKD", _clean(value)).encode("ascii", "ignore").decode()
    text = re.sub(r"\b(MESTO|MESTYS|OBEC|MESTSKA CAST)\b", " ", text.upper())
    return re.sub(r"[^A-Z0-9]+", "", text)


def _ico(value):
    raw = _clean(value)
    if not re.fullmatch(r"\d{1,8}(?:\.0)?", raw):
        return ""
    digits = raw.removesuffix(".0")
    return digits.zfill(8) if 1 <= len(digits) <= 8 else ""


def _date(value):
    if value is None or pd.isna(value) or value == "":
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).to_pydatetime()
    if isinstance(value, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=float(value))
    parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def _number(value):
    if value is None or pd.isna(value) or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def import_contracts(conn, uploaded_file, file_name, username):
    init_innogy(conn)
    try:
        frame = pd.read_excel(uploaded_file, dtype=object)
    except Exception as exc:
        raise InnogyImportError("Excel se nepodařilo přečíst.") from exc
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise InnogyImportError("V exportu chybí sloupce: " + ", ".join(sorted(missing)))

    municipalities = conn.execute("SELECT kod_obce,nazev,ico FROM obce").fetchall()
    by_ico, by_name = {}, {}
    for code, name, municipality_ico in municipalities:
        if _ico(municipality_ico):
            by_ico.setdefault(_ico(municipality_ico), []).append(code)
        by_name.setdefault(_normalize_name(name), []).append(code)

    import_id = hashlib.sha256(
        f"{file_name}|{datetime.now().isoformat()}".encode()
    ).hexdigest()[:24]
    matched = unmatched = 0
    for _, row in frame.iterrows():
        customer = _clean(row.get("Zákazník"))
        category = _clean(row.get("Kategorie")).upper()
        ico = _ico(row.get("Datum narození/IČO"))
        code, method = None, "Nespárováno"
        if ico and len(by_ico.get(ico, [])) == 1:
            code, method = by_ico[ico][0], "IČO"
        elif category != "DOM" and len(by_name.get(_normalize_name(customer), [])) == 1:
            code, method = by_name[_normalize_name(customer)][0], "Název"
        matched += int(code is not None)
        unmatched += int(code is None)

        ean = _clean(row.get("EAN/EIC"))
        generated = _date(row.get("Vygenerován Econ"))
        product = _clean(row.get("Produkt"))
        key_source = "|".join([ean, customer, generated.isoformat() if generated else "", product])
        contract_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        raw = {
            str(column): (None if pd.isna(value) else str(value))
            for column, value in row.items()
        }
        conn.execute("""
            INSERT INTO innogy_contracts (
                contract_key,import_id,kod_obce,match_method,ico,customer_name,
                ean_eic,opportunity_status,ex_code,seller_name,category,commodity,
                product,current_supplier,generated_at,signed_at,verified_at,
                verification_status,consumption_gas,consumption_high,consumption_low,
                termination_by,termination_method,phone,note,raw_data,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())
            ON CONFLICT (contract_key) DO UPDATE SET
                import_id=excluded.import_id,kod_obce=excluded.kod_obce,
                match_method=excluded.match_method,opportunity_status=excluded.opportunity_status,
                signed_at=excluded.signed_at,verified_at=excluded.verified_at,
                verification_status=excluded.verification_status,raw_data=excluded.raw_data,
                updated_at=now()
        """, [
            contract_key, import_id, code, method, ico, customer, ean,
            _clean(row.get("Status příležitosti")), _clean(row.get("EX kód")),
            _clean(row.get("Jméno prodejce")), category,
            _clean(row.get("Typ komodity")), product,
            _clean(row.get("Stávající dodavatel")), generated,
            _date(row.get("Podepsán Econ")) or _date(row.get("Datum podpisu")),
            _date(row.get("Datum ověření")), _clean(row.get("Status ověření")),
            _number(row.get("Spotřeba ZP")), _number(row.get("Spotřeba VT")),
            _number(row.get("Spotřeba NT")), _clean(row.get("Kdo podá výpověď")),
            _clean(row.get("Jak se má vypovědět")), _clean(row.get("Telefon")),
            _clean(row.get("Poznámka k příležitosti")),
            json.dumps(raw, ensure_ascii=False),
        ])

    conn.execute("""
        INSERT INTO innogy_imports
            (id,file_name,imported_by,total_rows,matched_rows,unmatched_rows)
        VALUES (?,?,?,?,?,?)
    """, [import_id, file_name, username, len(frame), matched, unmatched])
    return {"total": len(frame), "matched": matched, "unmatched": unmatched}
