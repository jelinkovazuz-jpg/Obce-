"""Tolerant extraction of calculation inputs from Czech energy invoice PDFs.

The parser intentionally returns partially extracted invoices.  Supplier layouts
change frequently, so uncertain values are shown for confirmation in the UI
instead of silently inventing a number or rejecting the whole invoice.
"""

from io import BytesIO
from datetime import date
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
        r"(?:Řádné\s+)?vyúčtování[^\n]*?za období\s*"
        r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})\s*[–—-]\s*"
        r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
        r"(?:Vyúčtování za období|Zúčtovací období|Fakturační období|Fakturované období)"
        r".*?(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})\s*[–—-]\s*"
        r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
        r"(?:období od)\s*(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})\s*(?:do|–|—|-)\s*"
        r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
    )
    candidates = []
    for pattern in labels:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            start, end = _date(match.group(1)), _date(match.group(2))
            if end >= start:
                candidates.append((start, end))
    if not candidates:
        raise InvoiceImportError("Na faktuře nebylo nalezeno fakturační období.")
    # Supplier invoices repeat shorter pricing subperiods later in the document.
    # The longest labelled interval is the actual settlement period.
    return max(candidates, key=lambda period: (period[1] - period[0]).days)


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


def _cez_details(text, commodity):
    """Read commercial values from current ČEZ settlement invoices."""
    section_match = re.search(
        r"Spotřebovan(?:á elektřina|ý plyn)\s*\(obchodní část\)(.*?)"
        r"(?:Distribuce (?:elektřiny|plynu)|Daň z (?:elektřiny|plynu) je)",
        text, re.IGNORECASE | re.DOTALL,
    )
    commercial = section_match.group(1) if section_match else text
    address_match = re.search(
        r"Adresa:\s*(.+?)\s+(?:EAN|EIC):\s*[A-Z0-9]+", text, re.IGNORECASE,
    )
    fee_matches = re.findall(
        r"Stálá platba\s+[\d .\u00a0]+,\d+\s+měs\.\s+"
        r"([\d .\u00a0]+,\d+)\s*Kč",
        commercial, re.IGNORECASE,
    )
    product_matches = re.findall(
        r"Produkt\s+((?:Elektřina|Plyn)[^\n]+)", commercial, re.IGNORECASE,
    )
    if commodity == "Elektřina":
        total_match = re.search(
            r"Celkové dodané množství elektřiny\s+([\d .\u00a0]+,\d+)\s*kWh",
            text, re.IGNORECASE,
        )
        split_match = re.search(
            r"Celkové distribuované množství elektřiny\s+"
            r"([\d .\u00a0]+,\d+)\s*kWh\s+([\d .\u00a0]+,\d+)\s*kWh",
            text, re.IGNORECASE,
        )
        price_rows = re.findall(
            r"Spotřeba\s*/\s*(vysoký|nízký) tarif[^\n]*?"
            r"[\d .\u00a0]+,\d+\s*MWh\s+([\d .\u00a0]+,\d+)\s*Kč",
            commercial, re.IGNORECASE,
        )
        if not total_match or not price_rows:
            return None
        actual = _number(total_match.group(1)) / 1000
        prices = {"VT": [], "NT": []}
        for tariff, price in price_rows:
            bucket = "VT" if tariff.casefold().startswith("vysok") else "NT"
            prices[bucket].append(_number(price))
        vt_quantity = _number(split_match.group(1)) if split_match else actual * 1000
        return {
            "actual_mwh": actual,
            "annual_mwh": None,
            "vt_share": vt_quantity / (actual * 1000) * 100 if actual else 100.0,
            "price_vt": prices["VT"][-1],
            "price_nt": prices["NT"][-1] if prices["NT"] else None,
            "monthly_fee": _number(fee_matches[-1]) if fee_matches else 0.0,
            "monthly_fee_found": bool(fee_matches),
            "product": product_matches[-1].strip() if product_matches else "",
            "address": address_match.group(1).strip() if address_match else "",
        }

    total_match = re.search(
        r"Celková spotřeba\s+([\d .\u00a0]+,\d+)\s*kWh", text, re.IGNORECASE,
    )
    annual_match = re.search(
        r"Spotřeba pro určení pásma:\s*([\d .\u00a0]+,\d+)\s*kWh",
        text, re.IGNORECASE,
    )
    price_rows = re.findall(
        r"^Spotřeba\s+[\d .\u00a0]+,\d+\s*MWh\s+"
        r"([\d .\u00a0]+,\d+)\s*Kč",
        commercial, re.IGNORECASE | re.MULTILINE,
    )
    if not total_match or not price_rows:
        return None
    actual = _number(total_match.group(1)) / 1000
    return {
        "actual_mwh": actual,
        "annual_mwh": _number(annual_match.group(1)) / 1000 if annual_match else None,
        "vt_share": 100.0,
        "price_vt": _number(price_rows[-1]),
        "price_nt": None,
        "monthly_fee": _number(fee_matches[-1]) if fee_matches else 0.0,
        "monthly_fee_found": bool(fee_matches),
        "product": product_matches[-1].strip() if product_matches else "",
        "address": address_match.group(1).strip() if address_match else "",
    }


def _mnd_details(text, commodity):
    """Read the authoritative commercial section of current MND invoices.

    Meter summaries can contain rollover/replacement values that are not billed
    consumption.  Only rows before ``Celková platba za obchodní část`` are used.
    """
    section_match = re.search(
        r"Obchodní část\s*-\s*ceník MND\s*\n(.*?)\nCelková platba za obchodní část",
        text, re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return None
    section = section_match.group(1)
    date_range = (
        r"\d{1,2}[.]\s*\d{1,2}[.]\s*\d{4}\s*-\s*"
        r"\d{1,2}[.]\s*\d{1,2}[.]\s*\d{4}"
    )
    items = []
    if commodity == "Elektřina":
        pattern = (
            rf"^(Proud\s*-\s*.+?)\s+(VT|NT)\s+{date_range}\s+MWh\s+"
            r"([\d .\u00a0]+,\d+)\s+([\d .\u00a0]+,\d+)\s*Kč\s+"
            r"[\d .\u00a0]+,\d+\s*Kč"
        )
        for match in re.finditer(pattern, section, re.IGNORECASE | re.MULTILINE):
            items.append({
                "product": match.group(1).strip(), "tariff": match.group(2).upper(),
                "quantity": _number(match.group(3)), "price": _number(match.group(4)),
            })
    else:
        pattern = (
            rf"^(Plyn\s+.+?)\s+{date_range}\s+MWh\s+"
            r"([\d .\u00a0]+,\d+)\s+([\d .\u00a0]+,\d+)\s*Kč\s+"
            r"[\d .\u00a0]+,\d+\s*Kč"
        )
        for match in re.finditer(pattern, section, re.IGNORECASE | re.MULTILINE):
            items.append({
                "product": match.group(1).strip(), "tariff": "ALL",
                "quantity": _number(match.group(2)), "price": _number(match.group(3)),
            })
    if not items:
        return None

    fee_matches = re.findall(
        rf"Měsíční platba\s+{date_range}\s+měsíc\s+"
        r"[\d .\u00a0]+,\d+\s+([\d .\u00a0]+,\d+)\s*Kč",
        section, re.IGNORECASE,
    )
    quantities = {
        tariff: sum(item["quantity"] for item in items if item["tariff"] == tariff)
        for tariff in ("VT", "NT", "ALL")
    }
    total = sum(item["quantity"] for item in items)
    prices = {
        tariff: [item["price"] for item in items if item["tariff"] == tariff]
        for tariff in ("VT", "NT", "ALL")
    }
    annual_match = re.search(
        r"Roční spotřeba pro přiřazení ceny\s*=\s*([\d .\u00a0]+,\d+)\s*MWh",
        text, re.IGNORECASE,
    )
    annual = _number(annual_match.group(1)) if annual_match else None
    address_match = re.search(
        r"Odběrné místo:\s*(?:EAN|EIC)\s+[A-Z0-9]+\s*\n([^\n]+)",
        text, re.IGNORECASE,
    )
    return {
        "actual_mwh": total,
        "annual_mwh": annual,
        "vt_share": 100.0 if total <= 0 else quantities["VT"] / total * 100,
        "price_vt": (prices["VT"] or prices["ALL"])[-1],
        "price_nt": prices["NT"][-1] if prices["NT"] else None,
        "monthly_fee": _number(fee_matches[-1]) if fee_matches else 0.0,
        "product": items[-1]["product"],
        "address": address_match.group(1).strip() if address_match else "",
    }


def _gas_band(annual_mwh):
    if annual_mwh <= 1.89:
        return "do 1,89 MWh"
    if annual_mwh <= 7.56:
        return "nad 1,89 do 7,56 MWh"
    if annual_mwh <= 63:
        return "nad 7,56 do 63 MWh"
    return "nad 63 MWh"


def _contract_address(text):
    section = re.search(
        r"ADRESA ODBĚRNÉHO MÍSTA\s+(.*?)(?:EAN|EIC) ODBĚRNÉHO MÍSTA:",
        text, re.IGNORECASE | re.DOTALL,
    )
    if not section:
        return ""
    value = section.group(1)
    street = _first((r"ULICE:\s*(.*?)\s+Č\.P\.:\s*([^\s]+)",), value)
    postal = _first((r"PSČ:\s*([\d ]{5,6})",), value)
    town = _first((r"OBEC:\s*(.*?)(?:\s+MÍSTNÍ ČÁST:|\n)",), value)
    if not (street and town):
        return ""
    street_text = f"{street.group(1).strip()} {street.group(2).strip()}"
    postal_text = postal.group(1).strip() if postal else ""
    return f"{street_text}, {postal_text} {town.group(1).strip()}".replace(",  ", ", ")


def _cez_contract_prices(text, rate_band):
    """Read VAT-exclusive commercial prices from a ČEZ contract price sheet."""
    header = re.search(
        r"Distribuční sazba\s+((?:[CD]\d{2}d\s+){2,}[CD]\d{2}d)",
        text, re.IGNORECASE,
    )
    if not header:
        return 0.0, None, 0.0
    rates = re.findall(r"[CD]\d{2}d", header.group(1), re.IGNORECASE)
    normalized = [rate[:-1].upper() + "d" for rate in rates]
    rate = rate_band[:-1].upper() + "d"
    if rate not in normalized:
        return 0.0, None, 0.0
    index = normalized.index(rate)
    money = r"([\d .\u00a0]+,\d+)"

    vt_section = re.search(
        r"Cena za dodávku(.*?)2\s+Nízký tarif", text,
        re.IGNORECASE | re.DOTALL,
    )
    nt_section = re.search(
        r"2\s+Nízký tarif(.*?)3\s+Stálá platba", text,
        re.IGNORECASE | re.DOTALL,
    )
    fee_section = re.search(
        r"3\s+Stálá platba(.*?)Distribuční část ceny", text,
        re.IGNORECASE | re.DOTALL,
    )
    vt_values = re.findall(rf"\({money}\)", vt_section.group(1)) if vt_section else []
    nt_values = re.findall(rf"\({money}\)", nt_section.group(1)) if nt_section else []
    fee_values = re.findall(rf"\({money}\)", fee_section.group(1)) if fee_section else []
    vt = _number(vt_values[index]) if index < len(vt_values) else 0.0
    # D01d/D02d have no NT, so the NT row contains two leading dashes rather
    # than two placeholder prices.
    nt_index = index - max(0, len(rates) - len(nt_values))
    nt = _number(nt_values[nt_index]) if 0 <= nt_index < len(nt_values) else None
    fee = _number(fee_values[index]) if index < len(fee_values) else 0.0
    return vt, nt, fee


def _parse_energy_contract_text(text, file_name):
    """Extract calculation inputs from an energy supply contract and its price sheet."""
    supplier = _detect_supplier(text)
    commodity = _detect_commodity(text)
    customer_match = _first((
        r"JMÉNO,\s*PŘÍJMENÍ:\s*([^\n]+)",
        r"NÁZEV(?: ZÁKAZNÍKA)?:\s*([^\n]+)",
    ), text)
    identifier = _identifier(text, commodity)
    rate_match = _first((
        r"DISTRIBUČNÍ SAZBA:\s*([CD]\d{2}[dD])",
        r"PÁSMO SPOTŘEBY:\s*([^\n]+)",
    ), text)
    rate_band = rate_match.group(1).strip() if rate_match else "Všechny"
    if re.fullmatch(r"[CD]\d{2}[dD]", rate_band):
        rate_band = rate_band[:-1].upper() + "d"
    consumption_match = re.search(
        r"PŘEDPOKLÁDANÁ SPOTŘEBA:\s*([\d .\u00a0]+(?:,\d+)?)\s*(MWh|kWh)\s*/\s*rok",
        text, re.IGNORECASE,
    )
    if not consumption_match:
        raise InvoiceImportError(
            "Ve smlouvě nebyla nalezena roční spotřeba. Doplňte fakturu se spotřebou "
            "nebo použijte smlouvu, která obsahuje předpokládanou spotřebu."
        )
    annual_mwh = _number(consumption_match.group(1))
    if consumption_match.group(2).lower() == "kwh":
        annual_mwh /= 1000

    price_vt = price_nt = monthly_fee = 0.0
    if supplier == "ČEZ Prodej, a.s." and commodity == "Elektřina":
        price_vt, price_nt, monthly_fee = _cez_contract_prices(text, rate_band)
    contract_type, contract_end = _contract(text)
    if re.search(r"(?:uzavírá|sjednána|smlouva je uzavřena)\s+se?\s*na dobu neurčitou", text, re.IGNORECASE):
        contract_type, contract_end = "Doba neurčitá", None
    notice_match = re.search(
        r"výpovědní doba je\s*(\d+|jeden|dva|tři|čtyři)\s+měsíc",
        text, re.IGNORECASE,
    )
    notice_words = {"jeden": 1, "dva": 2, "tři": 3, "čtyři": 4}
    notice_months = None
    if notice_match:
        raw_notice = notice_match.group(1).lower()
        notice_months = int(raw_notice) if raw_notice.isdigit() else notice_words[raw_notice]
    product_match = _first((
        r"Ceníkem produktu\s+([^,\n]+)",
        r"PRODUKT:\s*([^\n]+)",
    ), text)
    warnings = ["Spotřeba je předpoklad uvedený ve smlouvě, nikoli skutečná spotřeba z faktury."]
    if not identifier:
        warnings.append("Nepodařilo se přečíst EAN/EIC.")
    if price_vt <= 0:
        warnings.append("Z přiloženého ceníku se nepodařilo bezpečně určit obchodní cenu bez DPH.")
    if monthly_fee <= 0:
        warnings.append("Z přiloženého ceníku se nepodařilo bezpečně určit stálý obchodní plat bez DPH.")
    return {
        "file_name": file_name, "document_type": "Smlouva", "supplier": supplier,
        "customer": customer_match.group(1).strip() if customer_match else "",
        "address": _contract_address(text), "ean_eic": identifier,
        "commodity": commodity, "rate_band": rate_band,
        "billing_from": None, "billing_to": None,
        "actual_consumption_mwh": round(annual_mwh, 6),
        "annual_consumption_mwh": round(annual_mwh, 6),
        "consumption_source": "Předpokládaná roční spotřeba uvedená ve smlouvě",
        "vt_share": 100.0, "current_price_vt": price_vt,
        "current_price_nt": price_nt, "current_monthly_fee": monthly_fee,
        "contract_type": contract_type, "contract_end_date": contract_end,
        "notice_months": notice_months, "automatic_extension": False,
        "current_product": product_match.group(1).strip() if product_match else "",
        "warnings": warnings,
    }


def _centropol_details(text, commodity):
    """Extract commercial values from current Centropol settlements."""
    total_match = re.search(
        r"Vaše celková spotřeba (?:elektřiny|plynu)\s+"
        r"([\d .\u00a0]+,\d+)\s*(MWh|kWh)",
        text, re.IGNORECASE,
    )
    if not total_match:
        return None
    actual_mwh = _number(total_match.group(1))
    if total_match.group(2).lower() == "kwh":
        actual_mwh /= 1000

    quantities = {}
    for tariff, label in (("VT", "vysokém"), ("NT", "nízkem")):
        match = re.search(
            rf"Spotřeba ve {label} tarifu[^\n]*?Kč\s*/\s*kWh\s+"
            r"([\d .\u00a0]+,\d+)\s*kWh",
            text, re.IGNORECASE,
        )
        quantities[tariff] = _number(match.group(1)) / 1000 if match else 0.0

    prices = {"VT": [], "NT": []}
    for match in re.finditer(
        r"^Dodávky\s+(VT|NT)\s+([\d .\u00a0]+,\d+)\s*Kč\s*/\s*MWh\s+"
        r"[\d .\u00a0]+,\d+\s*MWh",
        text, re.IGNORECASE | re.MULTILINE,
    ):
        prices[match.group(1).upper()].append(_number(match.group(2)))

    discounts = {"VT": 0.0, "NT": 0.0}
    for match in re.finditer(
        r"Sleva\s+([\d.,]+)\s*%\s*-\s*spotřeba\s+(VT|NT)",
        text, re.IGNORECASE,
    ):
        discounts[match.group(2).upper()] = _number(match.group(1))

    def effective_price(tariff):
        if not prices[tariff]:
            return None
        return prices[tariff][-1] * (1 - discounts[tariff] / 100)

    fee_matches = re.findall(
        r"^Stálý měsíční plat\s+([\d .\u00a0]+,\d+)\s*Kč\s*/\s*měsíc",
        text, re.IGNORECASE | re.MULTILINE,
    )
    address_match = re.search(
        r"odběrné místo s kódem (?:EAN|EIC)\s+[A-Z0-9]+\s+na adrese\s+([^\n]+)",
        text, re.IGNORECASE,
    )
    rate_match = re.search(
        r"(?:Odhad spotřeby|Standardní odečet)\s+([CD]\d{2})D\b",
        text, re.IGNORECASE,
    )
    product_matches = re.findall(r"Produkt:\s*([^\n]+)", text, re.IGNORECASE)
    identifier = _identifier(text, commodity)
    contract_match = re.search(
        rf"{re.escape(identifier)}\s+"
        r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
        text, re.IGNORECASE,
    ) if identifier else None
    if identifier and not contract_match:
        contract_match = re.search(
            rf"{re.escape(identifier)}[^\n]*?ke dni\s+"
            r"(\d{1,2}\s*[.]\s*\d{1,2}\s*[.]\s*\d{4})",
            text, re.IGNORECASE,
        )
    return {
        "actual_mwh": actual_mwh,
        "vt_share": 100.0 if actual_mwh <= 0 else quantities["VT"] / actual_mwh * 100,
        "price_vt": effective_price("VT") or 0.0,
        "price_nt": effective_price("NT"),
        "monthly_fee": _number(fee_matches[-1]) if fee_matches else 0.0,
        "monthly_fee_found": bool(fee_matches),
        "address": address_match.group(1).strip() if address_match else "",
        "rate_band": rate_match.group(1).upper() + "d" if rate_match else "Všechny",
        "product": product_matches[-1].strip() if product_matches else "",
        "contract_end": _date(contract_match.group(1)) if contract_match else None,
    }


def merge_invoice_supply_points(points):
    """Combine non-overlapping invoice fragments belonging to the same EAN/EIC."""
    groups = {}
    for point in points:
        key = (point.get("ean_eic") or point.get("file_name", "")).strip().upper()
        groups.setdefault(key, []).append(point)
    merged = []
    for group in groups.values():
        group.sort(key=lambda item: (item.get("billing_to") or date.min))
        if len(group) == 1 or any(
            item.get("consumption_source") == "Roční spotřeba uvedená dodavatelem"
            for item in group
        ):
            merged.append(group[-1])
            continue
        periods_valid = all(
            item.get("billing_from") and item.get("billing_to") for item in group
        )
        overlaps = periods_valid and any(
            current["billing_from"] <= previous["billing_to"]
            for previous, current in zip(group, group[1:])
        )
        if not periods_valid or overlaps:
            merged.append(group[-1])
            continue
        latest = dict(group[-1])
        actual = sum(float(item["actual_consumption_mwh"]) for item in group)
        period_from = min(item["billing_from"] for item in group)
        period_to = max(item["billing_to"] for item in group)
        weighted_vt = sum(
            float(item["actual_consumption_mwh"]) * float(item.get("vt_share", 100))
            for item in group
        )
        latest.update({
            "file_name": " + ".join(item["file_name"] for item in group),
            "billing_from": period_from,
            "billing_to": period_to,
            "actual_consumption_mwh": round(actual, 6),
            "annual_consumption_mwh": round(
                annualize_consumption(actual, period_from, period_to), 6
            ),
            "consumption_source": f"Součet {len(group)} navazujících faktur",
            "vt_share": round(weighted_vt / actual, 4) if actual else 100.0,
            "warnings": sorted({
                warning for item in group for warning in item.get("warnings", [])
            }),
        })
        merged.append(latest)
    return sorted(merged, key=lambda item: (item["commodity"], item["ean_eic"]))


def parse_supplier_invoice_pdf(pdf_bytes, file_name="faktura.pdf"):
    """Backward-compatible single-point parser."""
    return parse_supplier_invoice_pdf_points(pdf_bytes, file_name)[0]


def parse_supplier_invoice_pdf_points(pdf_bytes, file_name="faktura.pdf"):
    """Return every supply point found in a supplier PDF."""
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
    if re.search(
        r"SMLOUVA\s+o\s+sdružených službách dodávky|SPECIFIKACE ODBĚRNÉHO MÍSTA",
        text, re.IGNORECASE,
    ):
        return [_parse_energy_contract_text(text, file_name)]
    if _detect_supplier(text) == "Pražská plynárenská, a.s.":
        points = _parse_ppas_points(text, file_name)
        if points:
            return points
    return [_parse_invoice_text(text, file_name)]


def _parse_ppas_points(text, file_name):
    """Split a multi-site Pražská plynárenská invoice into supply points."""
    customer_match = re.search(r"Fakturační adresa[^\n]*\n([^\n]+)", text, re.IGNORECASE)
    contract_groups = {}
    for contract_number, value in re.findall(
        r"(\d+(?:_\d+)?)\s+PLYN\s+INDIVIDUAL\s+"
        r"(\d{1,2}[.]\d{1,2}[.]\d{4})\s+"
        r"\d{1,2}[.]\d{1,2}[.]\d{4}",
        text, re.IGNORECASE,
    ):
        base_number = re.sub(r"_\d+$", "", contract_number)
        contract_groups[base_number] = max(
            contract_groups.get(base_number, _date(value)), _date(value)
        )
    contract_ends = list(contract_groups.values())
    blocks = [
        block for block in re.split(r"(?=ODBĚRNÉ MÍSTO:)", text)[1:]
        if re.search(r"EIC KÓD:\s*[A-Z0-9]+", block, re.IGNORECASE)
    ]
    points = []
    money = r"[\d]+(?:[ \u00a0][\d]{3})*,[\d]+"
    date_range = (
        r"(\d{1,2}[.]\d{1,2}[.]\d{4})\s*-\s*"
        r"(\d{1,2}[.]\d{1,2}[.]\d{4})"
    )
    for index, block in enumerate(blocks):
        eic_match = re.search(r"EIC KÓD:\s*([A-Z0-9]+)", block, re.IGNORECASE)
        address_match = re.search(r"ADRESA:\s*([^\n]+)", block, re.IGNORECASE)
        annual_match = re.search(
            r"PŘEPOČTENÁ ROČNÍ SPOTŘEBA\*\s*\(v kWh\):\s*([\d \u00a0]+)",
            block, re.IGNORECASE,
        )
        detail_match = re.search(
            r"DETAIL SPOTŘEBY:(.*?)Způsob odečtu:", block,
            re.IGNORECASE | re.DOTALL,
        )
        detail = detail_match.group(1) if detail_match else ""
        total_match = re.search(
            r"Celkem:\s+[\d \u00a0]+\s+([\d \u00a0]+,\d+)", detail,
            re.IGNORECASE,
        )
        periods = re.findall(date_range, detail)
        price_matches = re.findall(
            rf"^{date_range}\s+Komoditní složka ceny\s+{money}\s+MWh\s+"
            rf"{money}\s+({money})\s+{money}$",
            block, re.IGNORECASE | re.MULTILINE,
        )
        fee_matches = re.findall(
            rf"^{date_range}\s+(?:Stálý měsíční plat|Kapacitní složka ceny)\s+"
            rf"{money}(?:\s+Nm3)?\s+({money})\s+({money})\s+{money}$",
            block, re.IGNORECASE | re.MULTILINE,
        )
        if not (eic_match and address_match and annual_match and total_match and periods):
            continue
        billing_from = min(_date(start) for start, _ in periods)
        billing_to = max(_date(end) for _, end in periods)
        actual_mwh = _number(total_match.group(1))
        annual_mwh = _number(annual_match.group(1)) / 1000
        price = _number(price_matches[-1][-1]) if price_matches else 0.0
        monthly_fee = _number(fee_matches[-1][-1]) if fee_matches else 0.0
        contract_end = contract_ends[index] if index < len(contract_ends) else None
        warnings = []
        if price == 0:
            warnings.append("Nepodařilo se bezpečně určit obchodní cenu za MWh.")
        if contract_end is None:
            warnings.append("Faktura neuvádí jednoznačné datum konce smlouvy; smluvní údaje doplňte ručně.")
        points.append({
            "file_name": file_name,
            "supplier": "Pražská plynárenská, a.s.",
            "customer": customer_match.group(1).strip() if customer_match else "",
            "address": address_match.group(1).strip(),
            "ean_eic": eic_match.group(1).upper(),
            "commodity": "Plyn",
            "rate_band": _gas_band(annual_mwh),
            "billing_from": billing_from,
            "billing_to": billing_to,
            "actual_consumption_mwh": round(actual_mwh, 6),
            "annual_consumption_mwh": round(annual_mwh, 6),
            "consumption_source": "Roční spotřeba uvedená dodavatelem",
            "vt_share": 100.0,
            "current_price_vt": price,
            "current_price_nt": None,
            "current_monthly_fee": monthly_fee,
            "contract_type": "Doba určitá" if contract_end else "Doba neurčitá",
            "contract_end_date": contract_end,
            "automatic_extension": False,
            "current_product": "PLYN INDIVIDUAL",
            "warnings": warnings,
        })
    return points


def _parse_invoice_text(text, file_name):
    supplier = _detect_supplier(text)
    commodity = _detect_commodity(text)
    billing_from, billing_to = _billing_period(text)
    mnd = _mnd_details(text, commodity) if supplier == "MND Energie a.s." else None
    centropol = (
        _centropol_details(text, commodity)
        if supplier == "CENTROPOL ENERGY, a.s." else None
    )
    cez = _cez_details(text, commodity) if supplier == "ČEZ Prodej, a.s." else None
    supplier_details = mnd or centropol or cez
    actual_mwh = (
        supplier_details["actual_mwh"]
        if supplier_details else _consumption_mwh(text, commodity)
    )
    price_vt, price_nt = (
        (supplier_details["price_vt"], supplier_details["price_nt"])
        if supplier_details else _unit_prices(text, commodity)
    )
    contract_type, contract_end = _contract(text)
    if centropol and centropol["contract_end"]:
        contract_type, contract_end = "Doba určitá", centropol["contract_end"]
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
    if not address_match and not (supplier_details and supplier_details["address"]):
        warnings.append("Nepodařilo se přečíst adresu odběrného místa.")
    if price_vt == 0:
        warnings.append("Nepodařilo se bezpečně určit obchodní cenu za MWh.")
    monthly_fee = supplier_details["monthly_fee"] if supplier_details else _monthly_fee(text)
    fee_was_explicit = bool(supplier_details and supplier_details.get("monthly_fee_found"))
    if monthly_fee == 0 and not fee_was_explicit:
        warnings.append("Nepodařilo se bezpečně určit stálý obchodní měsíční plat.")
    if contract_end is None and not re.search(r"na dobu neurčitou", text, re.IGNORECASE):
        warnings.append("Faktura neuvádí jednoznačné datum konce smlouvy; smluvní údaje doplňte ručně.")

    days = (billing_to - billing_from).days + 1
    rate_band = rate_match.group(1).strip() if rate_match else "Všechny"
    if centropol:
        rate_band = centropol["rate_band"]
    if re.fullmatch(r"[CD]\d{2}[dD]", rate_band):
        rate_band = rate_band[:-1] + "d"
    annual_mwh = annualize_consumption(
        actual_mwh, billing_from, billing_to,
        stated_annual=supplier_details.get("annual_mwh") if supplier_details else None,
    )
    if commodity == "Plyn" and supplier_details:
        rate_band = _gas_band(annual_mwh)
    address = supplier_details["address"] if supplier_details and supplier_details["address"] else (
        address_match.group(1).strip() if address_match else ""
    )
    product = supplier_details["product"] if supplier_details else (
        product_match.group(1).strip() if product_match else ""
    )
    return {
        "file_name": file_name, "supplier": supplier,
        "customer": customer_match.group(1).strip() if customer_match else "",
        "address": address,
        "ean_eic": ean_eic, "commodity": commodity,
        "rate_band": rate_band,
        "billing_from": billing_from, "billing_to": billing_to,
        "actual_consumption_mwh": round(actual_mwh, 6),
        "annual_consumption_mwh": round(annual_mwh, 6),
        "consumption_source": f"Skutečná spotřeba za {days} dní",
        "vt_share": round(supplier_details["vt_share"], 4) if supplier_details else 100.0,
        "current_price_vt": price_vt,
        "current_price_nt": price_nt, "current_monthly_fee": monthly_fee,
        "contract_type": contract_type, "contract_end_date": contract_end,
        "automatic_extension": bool(re.search(r"automatick(?:é|ému) prodloužení", text, re.IGNORECASE)),
        "current_product": product,
        "warnings": warnings,
    }
