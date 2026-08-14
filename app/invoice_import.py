"""Extraction of calculation inputs from supplier invoice PDFs."""

from datetime import date
from io import BytesIO
import re

import pandas as pd

from app.energy_calculator import EnergyCalculationError, annualize_consumption


class InvoiceImportError(EnergyCalculationError):
    pass


def _number(value):
    return float(value.replace("\u00a0", "").replace(" ", "").replace(",", "."))


def _date(value):
    return pd.to_datetime(value.replace(" ", ""), dayfirst=True).date()


def _required(pattern, text, label, flags=re.IGNORECASE):
    match = re.search(pattern, text, flags)
    if not match:
        raise InvoiceImportError(f"Na faktuře nebyl nalezen údaj: {label}.")
    return match


def parse_supplier_invoice_pdf(pdf_bytes, file_name="faktura.pdf"):
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text(x_tolerance=2, y_tolerance=3) or "" for page in pdf.pages]
    except Exception as exc:
        raise InvoiceImportError(f"Fakturu {file_name} se nepodařilo přečíst.") from exc
    text = "\n".join(pages)
    if "ČEZ Prodej" in text and "VYÚČTOVÁNÍ ZA ELEKTŘINU" in text:
        return _parse_cez_electricity(text, file_name)
    raise InvoiceImportError(
        f"Formát faktury {file_name} zatím není podporovaný. Aktuálně umím faktury ČEZ za elektřinu."
    )


def _parse_cez_electricity(text, file_name):
    billing = _required(
        r"Vyúčtování za období.*?\n\s*(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\s*[–-]\s*"
        r"(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})",
        text, "fakturační období", flags=re.IGNORECASE | re.DOTALL,
    )
    billing_from, billing_to = map(_date, billing.groups())
    ean = _required(r"EAN(?:\s+kód|:)?\s*(\d{18})", text, "EAN").group(1)
    address = _required(r"Adresa:\s*(.+?)\s+EAN:\s*\d{18}", text, "adresa odběrného místa").group(1).strip()
    rate = _required(r"Distribuční sazba\s+([CD]\d{2}[Dd])", text, "distribuční sazba").group(1)
    rate = rate[:-1] + "d"
    consumption_kwh = _number(_required(
        r"Celkové dodané množství elektřiny\s+([\d \u00a0]+,\d+)\s*kWh",
        text, "celková spotřeba",
    ).group(1))
    actual_mwh = consumption_kwh / 1000
    annual_mwh = annualize_consumption(actual_mwh, billing_from, billing_to)

    vt_items = re.findall(
        r"Spotřeba\s*/\s*vysoký tarif[^\n]*?([\d ]+,\d+)\s*MWh\s+"
        r"([\d ]+,\d+)\s*Kč",
        text, flags=re.IGNORECASE,
    )
    if not vt_items:
        raise InvoiceImportError("Na faktuře nebyla nalezena obchodní cena VT.")
    current_price_vt = _number(vt_items[-1][1])
    nt_items = re.findall(
        r"Spotřeba\s*/\s*nízký tarif[^\n]*?([\d ]+,\d+)\s*MWh\s+"
        r"([\d ]+,\d+)\s*Kč",
        text, flags=re.IGNORECASE,
    )
    current_price_nt = _number(nt_items[-1][1]) if nt_items else None

    history = re.search(
        r"Historie spotřeby.*?\n.*?\n"
        r"\d{1,2}\.\s*\d{1,2}\.\s*\d{4}\s*[–-]\s*\d{1,2}\.\s*\d{1,2}\.\s*\d{4}"
        r"\s+\d+\s+([\d ]+)\s*kWh\s+([\d ]+)\s*kWh",
        text, flags=re.IGNORECASE | re.DOTALL,
    )
    if history:
        vt_kwh, nt_kwh = (_number(value) for value in history.groups())
    else:
        vt_kwh, nt_kwh = consumption_kwh, 0
    total_tariffs = vt_kwh + nt_kwh
    vt_share = 100 if total_tariffs <= 0 else vt_kwh / total_tariffs * 100

    fees = re.findall(
        r"Stálá platba\s+[\d ]+,\d+\s*měs\.\s+([\d ]+,\d+)\s*Kč",
        text, flags=re.IGNORECASE,
    )
    if not fees:
        raise InvoiceImportError("Na faktuře nebyl nalezen stálý obchodní plat.")
    monthly_fee = _number(fees[-1])
    contract = _required(
        r"Smlouva je sjednána na dobu určitou do dne\s+"
        r"(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})",
        text, "datum konce smlouvy",
    )
    contract_end = _date(contract.group(1))
    customer_match = re.search(r"ČÁST A\s+ZÁKAZNÍK\s*\n([^\n]+)", text, re.IGNORECASE)
    customer = customer_match.group(1).strip() if customer_match else ""
    product_matches = re.findall(r"Produkt\s+([^\n]+)", text, re.IGNORECASE)
    product = product_matches[-1].strip() if product_matches else ""

    return {
        "file_name": file_name, "supplier": "ČEZ Prodej, a.s.",
        "customer": customer, "address": address, "ean_eic": ean,
        "commodity": "Elektřina", "rate_band": rate,
        "billing_from": billing_from, "billing_to": billing_to,
        "actual_consumption_mwh": round(actual_mwh, 6),
        "annual_consumption_mwh": round(annual_mwh, 6),
        "consumption_source": f"Skutečná spotřeba za {((billing_to - billing_from).days + 1)} dní",
        "vt_share": round(vt_share, 4),
        "current_price_vt": current_price_vt,
        "current_price_nt": current_price_nt,
        "current_monthly_fee": monthly_fee,
        "contract_type": "Doba určitá", "contract_end_date": contract_end,
        "automatic_extension": bool(re.search(r"automatickému prodloužení", text, re.IGNORECASE)),
        "current_product": product,
    }
