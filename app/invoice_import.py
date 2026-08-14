"""Tolerant extraction of calculation inputs from Czech energy invoice PDFs.

The parser intentionally returns partially extracted invoices.  Supplier layouts
change frequently, so uncertain values are shown for confirmation in the UI
instead of silently inventing a number or rejecting the whole invoice.
"""

from io import BytesIO
import re

import pandas as pd

from app.energy_calculator import EnergyCalculationError, annualize_consumption


class InvoiceImportError(EnergyCalculationError):
    pass


SUPPLIERS = (
    ("ČEZ Prodej, a.s.", ("ČEZ Prodej", "SKUPINA ČEZ", "cez.cz")),
    ("MND Energie a.s.", ("MND Energie", "Moje MND", "mnd.cz")),
    ("EP ENERGY TRADING, a.s. (epet)", ("EP ENERGY TRADING", "epet.cz")),
    ("E.ON Energie, a.s.", ("E.ON Energie", "eon.cz")),
    ("innogy Energie, s.r.o.", ("innogy Energie", "innogy.cz")),
    ("Pražská energetika, a.s. (PRE)", ("Pražská energetika", "pre.cz")),
    ("CENTROPOL ENERGY, a.s.", ("CENTROPOL ENERGY", "centropol.cz")),
    ("Pražská plynárenská, a.s.", ("Pražská plynárenská", "ppas.cz")),
    ("TEDOM energie s.r.o.", ("TEDOM energie", "tedomenergie.cz")),
)


def _number(value):
    value = value.replace("\u00a0", "").replace(" ", "").replace("Kč", "")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    return float(value)


def _date(value):
    return pd.to_datetime(value.replace(" ", ""), dayfirst=True).date()


def _first(patterns, text, flags=re.IGNORECASE):
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match
    return None


def _detect_supplier(text):
    folded = text.casefold()
    for supplier, markers in SUPPLIERS:
        if any(marker.casefold() in folded for marker in markers):
            return supplier
    match = _first((r"Dodavatel\s*:?\s*([^\n|]+)", r"DODAVATEL\s*\n([^\n]+)"), text)
    return match.group(1).strip() if match else "Nerozpoznaný dodavatel"


def _detect_commodity(text):
    gas = len(re.findall(r"\b(?:plyn|plynu|plynoměr|EIC)\b", text, re.IGNORECASE))
    electricity = len(re.findall(
        r"\b(?:elektřina|elektřiny|elektroměr|EAN|vysoký tarif|nízký tarif)\b",
        text, re.IGNORECASE,
    ))
    if gas == electricity == 0:
        raise InvoiceImportError("Na faktuře se nepodařilo rozpoznat komoditu.")
    return "Plyn" if gas > electricity else "Elektřina"


def _billing_period(text):
    labels = (
        r"(?:Vyúčtování za období|Zúčtovací období|Fakturační období|Fakturované období)"
        r".*?(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})\s*[–—-]\s*"
        r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
        r"(?:období od)\s*(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})\s*(?:do|–|—|-)\s*"
        r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
    )
    match = _first(labels, text, re.IGNORECASE | re.DOTALL)
    if not match:
        raise InvoiceImportError("Na faktuře nebylo nalezeno fakturační období.")
    return _date(match.group(1)), _date(match.group(2))


def _identifier(text, commodity):
    label = "EAN" if commodity == "Elektřina" else "EIC"
    match = _first((
        rf"{label}(?:\s+kód(?:em)?)?\s*:?\s*([A-Z0-9 ]{{16,24}})",
        r"\b(859\d{15})\b" if commodity == "Elektřina" else r"\b(27ZG[A-Z0-9]{12})\b",
    ), text)
    if not match:
        return ""
    value = re.sub(r"\s+", "", match.group(1).upper())
    return value[:18] if commodity == "Elektřina" else value[:16]


def _consumption_mwh(text, commodity):
    patterns = (
        r"Celkov(?:é|á) (?:dodané množství elektřiny|dodávka elektřiny[^\n]*?|spotřeba(?: zemního)? plynu)"
        r"[^\d]{0,100}([\d .\u00a0]+,\d+)\s*(MWh|kWh)",
        r"Spotřeba(?: v)?\s*(?:činí|celkem|:)\s*([\d .\u00a0]+,\d+)\s*(MWh|kWh)",
    )
    match = _first(patterns, text, re.IGNORECASE | re.DOTALL)
    if match:
        value = _number(match.group(1))
        return value / 1000 if match.group(2).lower() == "kwh" else value

    # Sum tariff consumption only when no explicit total exists.
    if commodity == "Elektřina":
        values = re.findall(
            r"(?:Spotřeba|Dodávka)[^\n]{0,50}(?:VT|NT|vysoký tarif|nízký tarif)"
            r"[^\n]{0,80}?([\d .\u00a0]+,\d+)\s*MWh",
            text, re.IGNORECASE,
        )
        if values:
            return sum(_number(value) for value in values)
    raise InvoiceImportError("Na faktuře nebyla nalezena celková spotřeba v MWh nebo kWh.")


def _unit_prices(text, commodity):
    prices = {"VT": [], "NT": [], "ALL": []}
    for line in text.splitlines():
        if not re.search(r"dodáv|spotřeb|silov|odebran", line, re.IGNORECASE):
            continue
        explicit = re.findall(
            r"([\d .\u00a0]+,\d+)\s*(?:Kč\s*/\s*MWh|Kč/MWh)|"
            r"\d[\d .\u00a0]*,\d+\s*MWh\s+([\d .\u00a0]+,\d+)\s*Kč",
            line, re.IGNORECASE,
        )
        candidates = re.findall(r"([\d .\u00a0]+,\d+)\s*(?:Kč)?\s*/?\s*MWh|([\d .\u00a0]+,\d+)\s*Kč", line)
        numbers = [_number(a or b) for a, b in explicit] or [_number(a or b) for a, b in candidates]
        # Unit price is normally the last plausible Kč/MWh value before line total.
        plausible = [number for number in numbers if 50 <= number <= 100000]
        if not plausible:
            continue
        bucket = "NT" if re.search(r"\bNT\b|nízký tarif", line, re.IGNORECASE) else (
            "VT" if re.search(r"\bVT\b|vysoký tarif", line, re.IGNORECASE) else "ALL"
        )
        prices[bucket].append(plausible[0] if explicit else plausible[-1])
    vt = (prices["VT"] or prices["ALL"])
    nt = prices["NT"]
    return (vt[-1] if vt else 0.0), (nt[-1] if nt else None)


def _monthly_fee(text):
    values = []
    for line in text.splitlines():
        if not re.search(r"stál(?:á|ý)|měsíční (?:plat|poplatek)", line, re.IGNORECASE):
            continue
        matches = re.findall(r"([\d .\u00a0]+,\d+)\s*Kč", line)
        if matches:
            nums = [_number(value) for value in matches]
            plausible = [number for number in nums if 0 <= number <= 5000]
            if plausible:
                values.append(plausible[-1] if len(plausible) == 1 else plausible[-2])
    return values[-1] if values else 0.0


def _contract(text):
    fixed = _first((
        r"(?:sjednána|uzavřena|platí)\s+na dobu určitou(?:\s+do dne)?\s*(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
        r"(?:konec|doba trvání|platnost) smlouvy[^\d]{0,40}(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
    ), text)
    if fixed:
        return "Doba určitá", _date(fixed.group(1))
    return "Doba neurčitá", None


def parse_supplier_invoice_pdf(pdf_bytes, file_name="faktura.pdf"):
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text(x_tolerance=2, y_tolerance=3) or "" for page in pdf.pages]
    except Exception as exc:
        raise InvoiceImportError(f"Fakturu {file_name} se nepodařilo přečíst.") from exc
    text = "\n".join(pages)
    if len(text.strip()) < 80:
        raise InvoiceImportError(
            f"Faktura {file_name} neobsahuje čitelný text. Nahrajte původní PDF, ne naskenovaný obrázek."
        )
    return _parse_invoice_text(text, file_name)


def _parse_invoice_text(text, file_name):
    supplier = _detect_supplier(text)
    commodity = _detect_commodity(text)
    billing_from, billing_to = _billing_period(text)
    actual_mwh = _consumption_mwh(text, commodity)
    price_vt, price_nt = _unit_prices(text, commodity)
    contract_type, contract_end = _contract(text)
    ean_eic = _identifier(text, commodity)

    address_match = _first((
        r"Adresa(?: odběrného místa)?\s*:\s*(.+?)(?:\s+EAN|\s+EIC|\n)",
        r"Odběrné místo\s*:?\s*\n?([^\n]+)",
    ), text)
    rate_match = _first((
        r"Distribuční sazba\s*:?\s*([CD]\d{2}[dD])",
        r"Pásmo spotřeby\s*:?\s*([^\n]+)",
    ), text)
    customer_match = _first((
        r"ČÁST A\s+ZÁKAZNÍK\s*\n([^\n]+)",
        r"ZÁKAZNÍK\s*\n([^\n]+)",
    ), text)
    product_match = _first((r"Produkt\s*:?\s*([^\n]+)", r"Název ceníku\s*:?\s*([^\n]+)"), text)

    warnings = []
    if not ean_eic:
        warnings.append("Nepodařilo se přečíst EAN/EIC.")
    if not address_match:
        warnings.append("Nepodařilo se přečíst adresu odběrného místa.")
    if price_vt == 0:
        warnings.append("Nepodařilo se bezpečně určit obchodní cenu za MWh.")
    monthly_fee = _monthly_fee(text)
    if monthly_fee == 0:
        warnings.append("Nepodařilo se bezpečně určit stálý obchodní měsíční plat.")
    if contract_end is None:
        warnings.append("Faktura neuvádí jednoznačné datum konce smlouvy; smluvní údaje doplňte ručně.")

    days = (billing_to - billing_from).days + 1
    rate_band = rate_match.group(1).strip() if rate_match else "Všechny"
    if re.fullmatch(r"[CD]\d{2}[dD]", rate_band):
        rate_band = rate_band[:-1] + "d"
    return {
        "file_name": file_name, "supplier": supplier,
        "customer": customer_match.group(1).strip() if customer_match else "",
        "address": address_match.group(1).strip() if address_match else "",
        "ean_eic": ean_eic, "commodity": commodity,
        "rate_band": rate_band,
        "billing_from": billing_from, "billing_to": billing_to,
        "actual_consumption_mwh": round(actual_mwh, 6),
        "annual_consumption_mwh": round(annualize_consumption(actual_mwh, billing_from, billing_to), 6),
        "consumption_source": f"Skutečná spotřeba za {days} dní",
        "vt_share": 100.0, "current_price_vt": price_vt,
        "current_price_nt": price_nt, "current_monthly_fee": monthly_fee,
        "contract_type": contract_type, "contract_end_date": contract_end,
        "automatic_extension": bool(re.search(r"automatick(?:é|ému) prodloužení", text, re.IGNORECASE)),
        "current_product": product_match.group(1).strip() if product_match else "",
        "warnings": warnings,
    }
