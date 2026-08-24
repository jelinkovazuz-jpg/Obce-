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

    def test_current_mnd_electricity_commercial_rows_are_summed(self):
        text = """MND Energie a.s. Moje MND
Obec Přelovice
Odběrné místo: EAN 859182400708742955
Přelovice Parc. č. 29/3, Přelovice (přípojka Marina)
Distribuční sazba: C02d Jistič: 3x25 A
Fakturované období Stav Počáteční Koncový Spotřeba
26. 10. 2024 - 28. 10. 2025 VT (T1) 366 kWh 608 kWh 242 kWh
Vaše smlouva je na dobu neurčitou s tříměsíční výpovědní dobou.
Obchodní část - ceník MND
Období Jednotka Množství Za jednotku Celkem bez DPH
Proud - Maloodběratelé VT 26. 10. 2024 - 31. 12. 2024 MWh 0,04600 3 864,46 Kč 177,77 Kč
Proud - Maloodběratelé VT 01. 01. 2025 - 28. 10. 2025 MWh 0,19600 3 464,46 Kč 679,04 Kč
Měsíční platba 26. 10. 2024 - 31. 12. 2024 měsíc 2,19400 63,64 Kč 139,63 Kč
Měsíční platba 01. 01. 2025 - 28. 10. 2025 měsíc 9,90300 99,17 Kč 982,08 Kč
Daň z elektřiny 26. 10. 2024 - 28. 10. 2025 MWh 0,24200 28,30 Kč 6,85 Kč
Celková platba za obchodní část 1 985,37 Kč
"""
        result = _parse_invoice_text(text, "mnd-elektrina.pdf")
        self.assertEqual(result["actual_consumption_mwh"], 0.242)
        self.assertEqual(result["current_price_vt"], 3464.46)
        self.assertEqual(result["current_monthly_fee"], 99.17)
        self.assertEqual(result["address"], "Přelovice Parc. č. 29/3, Přelovice (přípojka Marina)")
        self.assertEqual(result["contract_type"], "Doba neurčitá")

    def test_current_mnd_gas_uses_stated_annual_consumption_and_band(self):
        text = """MND Energie a.s. Moje MND
Obec Přelovice
Odběrné místo: EIC 27ZG500Z0069510I
Přelovice 87, Přelovice (Budova OÚ)
Fakturované období Počáteční stav Koncový stav Spotřeba
27. 07. 2025 - 28. 07. 2026 831 m³ 1 878 m³ 1 047 m³
Vaše smlouva je na dobu neurčitou s tříměsíční výpovědní dobou.
Roční spotřeba pro přiřazení ceny = 11,81463 MWh
Obchodní část - ceník MND
Období Jednotka Množství Za jednotku Celkem bez DPH
Plyn z první ruky s 6% prémií za věrnost 27. 07. 2025 - 28. 07. 2026 MWh 11,81754 1 327,27 Kč 15 685,07 Kč
Měsíční platba 27. 07. 2025 - 28. 07. 2026 měsíc 12,06452 138,84 Kč 1 675,03 Kč
Daň z plynu 27. 07. 2025 - 28. 07. 2026 MWh 11,81754 30,60 Kč 361,62 Kč
Celková platba za obchodní část 17 721,72 Kč
"""
        result = _parse_invoice_text(text, "mnd-plyn.pdf")
        self.assertEqual(result["actual_consumption_mwh"], 11.81754)
        self.assertEqual(result["annual_consumption_mwh"], 11.81463)
        self.assertEqual(result["rate_band"], "nad 7,56 do 63 MWh")
        self.assertEqual(result["current_price_vt"], 1327.27)
        self.assertEqual(result["current_monthly_fee"], 138.84)

    def test_centropol_invoice_extracts_consumption_tariffs_and_discounted_prices(self):
        text = """VYÚČTOVÁNÍ ZA ELEKTŘINU
Řádné vyúčtování za sdružené služby dodávky elektřiny za období 01.12.2025 - 11.08.2026
DODAVATEL: CENTROPOL ENERGY, a.s. www.centropol.cz
Souhrnné informace Vašeho vyúčtování pro odběrné místo s kódem EAN 859182400700852669 na adrese Kunčice 100, Kunčice
Vaše celková spotřeba elektřiny 10,3250 MWh
Spotřeba ve vysokém tarifu (VT/T1) 5,12 Kč/kWh 7 431,00 kWh 38 024,42 Kč
Spotřeba v nízkem tarifu (NT/T2) 2,78 Kč/kWh 2 894,00 kWh 8 035,72 Kč
Odhad spotřeby C25D (TDD2) 1,0 29 363 - 30 409 kWh
Období 01.08.2026 - 11.08.2026 Produkt: FIXNĚ PRO PODNIKATELE na 2 roky
Dodávky VT 2 775,00 Kč/MWh 0,26700 MWh 740,93 Kč
Dodávky NT 2 510,00 Kč/MWh 0,10400 MWh 261,04 Kč
Stálý měsíční plat 130,00 Kč/měsíc 0,3548 měsíc 46,12 Kč
Sleva 5 % - spotřeba NT -2,61 Kč 5 % -13,05 Kč
Sleva 5 % - spotřeba VT -7,41 Kč 5 % -37,05 Kč
EAN/EIC Závazek smlouvy do Produkt
859182400700852669 30.11.2027 FIXNĚ PRO PODNIKATELE
"""
        result = _parse_invoice_text(text, "centropol.pdf")
        self.assertEqual(result["actual_consumption_mwh"], 10.325)
        self.assertAlmostEqual(result["vt_share"], 71.9709, places=4)
        self.assertEqual(result["current_price_vt"], 2636.25)
        self.assertEqual(result["current_price_nt"], 2384.5)
        self.assertEqual(result["current_monthly_fee"], 130)
        self.assertEqual(result["rate_band"], "C25d")
        self.assertEqual(result["address"], "Kunčice 100, Kunčice")
        self.assertEqual(result["contract_end_date"], date(2027, 11, 30))

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
