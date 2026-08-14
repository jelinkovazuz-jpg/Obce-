import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app.invoice_import import _parse_invoice_text, parse_supplier_invoice_pdf
from app.energy_ui import _invoice_row
from app.energy_calculator import init_energy_calculator
import duckdb


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

    def test_epet_invoice_is_recognized_and_partial_fields_are_editable(self):
        text = """FAKTURA ZA DODÁVKU ELEKTŘINY
EP ENERGY TRADING, a.s. www.epet.cz
Zúčtovací období 17.02.2017 - 15.02.2018
EAN: 859182400700227603
Celková dodávka elektřiny do odběrných míst za zúčtovací období činí 4,48300 MWh
"""
        result = _parse_invoice_text(text, "epet.pdf")
        self.assertEqual(result["supplier"], "EP ENERGY TRADING, a.s. (epet)")
        self.assertEqual(result["commodity"], "Elektřina")
        self.assertEqual(result["actual_consumption_mwh"], 4.483)
        self.assertEqual(result["ean_eic"], "859182400700227603")
        self.assertTrue(result["warnings"])

    def test_mnd_gas_common_fields_are_extracted(self):
        text = """VYÚČTOVÁNÍ ZA PLYN
MND Energie a.s. Moje MND
Fakturační období: 1. 1. 2025 - 31. 12. 2025
EIC: 27ZG600Z12345678
Adresa odběrného místa: Náměstí 1, Testov EIC: 27ZG600Z12345678
Celková spotřeba plynu: 12 500,00 kWh
Dodávka plynu 12,500 MWh 1 250,00 Kč/MWh 15 625,00 Kč
Stálý měsíční plat 130,00 Kč 1 560,00 Kč
"""
        result = _parse_invoice_text(text, "mnd-plyn.pdf")
        self.assertEqual(result["supplier"], "MND Energie a.s.")
        self.assertEqual(result["commodity"], "Plyn")
        self.assertEqual(result["actual_consumption_mwh"], 12.5)
        self.assertEqual(result["ean_eic"], "27ZG600Z12345678")
        self.assertEqual(result["current_price_vt"], 1250)
        self.assertEqual(result["current_monthly_fee"], 130)

    def test_expired_fixed_contract_uses_future_notice_period(self):
        conn = duckdb.connect(":memory:")
        init_energy_calculator(conn)
        extracted = _parse_invoice_text(CEZ_TEXT, "cez.pdf")
        row = _invoice_row(conn, extracted, date(2026, 8, 14))
        self.assertEqual(row["Typ smlouvy"], "Doba určitá")
        self.assertEqual(row["Konec smlouvy"], date(2027, 5, 24))
        self.assertEqual(row["Začátek innogy"], date(2027, 5, 25))
        self.assertIn("prodloužení o 12 měsíců", row["Upozornění"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
