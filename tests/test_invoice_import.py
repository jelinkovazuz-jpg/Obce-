import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app.invoice_import import parse_supplier_invoice_pdf


CEZ_TEXT = """VYÚČTOVÁNÍ ZA ELEKTŘINU
ČÁST A
ZÁKAZNÍK
Městys Choltice
Vyúčtování za období
27. 6. 2024 – 24. 6. 2025
Distribuční sazba C01D
ČEZ Prodej, a.s.
Adresa: Ledec 29, 535 01 Choltice EAN: 859182400700227603
Celkové dodané množství elektřiny 797,00 kWh
Spotřeba / vysoký tarif (VT/T1) 0,35600 MWh 3 462,81 Kč 1 232,76 Kč
Spotřeba / vysoký tarif (VT/T1) 0,07028 MWh 3 475,21 Kč 244,24 Kč
Stálá platba 6,13300 měs. 128,00 Kč 785,02 Kč
Stálá platba 1,02600 měs. 128,00 Kč 131,33 Kč
Produkt Elektřina Bez starostí na 1 rok
INFORMACE O TRVÁNÍ SMLOUVY
Smlouva je sjednána na dobu určitou do dne 24. 5. 2026. Dne 25. 5. 2026 dojde k jejímu automatickému prodloužení.
"""


class FakePdf:
    pages = [SimpleNamespace(extract_text=lambda **_: CEZ_TEXT)]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class InvoiceImportTest(unittest.TestCase):
    def test_cez_invoice_fields_are_extracted(self):
        fake_module = SimpleNamespace(open=lambda _: FakePdf())
        with patch.dict("sys.modules", {"pdfplumber": fake_module}):
            result = parse_supplier_invoice_pdf(b"pdf", "cez.pdf")
        self.assertEqual(result["customer"], "Městys Choltice")
        self.assertEqual(result["ean_eic"], "859182400700227603")
        self.assertEqual(result["rate_band"], "C01d")
        self.assertEqual(result["annual_consumption_mwh"], 0.797)
        self.assertEqual(result["current_price_vt"], 3475.21)
        self.assertEqual(result["current_monthly_fee"], 128)
        self.assertEqual(result["contract_end_date"], date(2026, 5, 24))
        self.assertTrue(result["automatic_extension"])


if __name__ == "__main__":
    unittest.main()
