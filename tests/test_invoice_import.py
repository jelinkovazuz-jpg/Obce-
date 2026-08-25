import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app.invoice_import import (
    _parse_invoice_text,
    _parse_ppas_points,
    merge_invoice_supply_points,
    parse_supplier_invoice_pdf,
)
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
    def test_cez_header_period_beats_shorter_pricing_subperiod(self):
        text = """ČEZ Prodej, a.s. VYÚČTOVÁNÍ ZA ELEKTŘINU
Vyúčtování za období Variabilní symbol Datum splatnosti Produkt Elektřina pro ZTP
12. 3. 2025 – 4. 3. 2026 4102077600 23. 3. 2026 Distribuční sazba D25D
ČÁST A
ZÁKAZNÍK
Jan Sedláček
Adresa: U vodárny 1059, 503 46 Třebechovice pod Orebem EAN: 859182400706305275
Celkové distribuované množství elektřiny 2 973,00 kWh 1 024,00 kWh
Celkové dodané množství elektřiny 3 997,00 kWh
Spotřeba / vysoký tarif (VT/T1) 2,49300 MWh 3 270,25 Kč 8 152,73 Kč
Spotřeba / nízký tarif (NT/T2) 0,84600 MWh 3 071,90 Kč 2 598,83 Kč
1. 1. 2026 - 4. 3. 2026 Produkt Elektřina pro ZTP
Spotřeba / vysoký tarif (VT/T1) 0,48000 MWh 3 072,73 Kč 1 474,91 Kč
Spotřeba / nízký tarif (NT/T2) 0,17800 MWh 2 857,85 Kč 508,70 Kč
Stálá platba 2,12900 měs. 76,00 Kč 161,80 Kč
INFORMACE O TRVÁNÍ SMLOUVY
Smlouva je sjednána na dobu neurčitou.
"""
        result = _parse_invoice_text(text, "cez-elektrina.pdf")
        self.assertEqual(result["billing_from"], date(2025, 3, 12))
        self.assertEqual(result["billing_to"], date(2026, 3, 4))
        self.assertEqual(result["annual_consumption_mwh"], 3.997)
        self.assertAlmostEqual(result["vt_share"], 74.3808, places=4)
        self.assertEqual(result["current_price_vt"], 3072.73)
        self.assertEqual(result["current_price_nt"], 2857.85)
        self.assertEqual(result["current_monthly_fee"], 76)

    def test_cez_gas_invoice_uses_stated_annual_consumption(self):
        text = """ČEZ Prodej, a.s. VYÚČTOVÁNÍ ZA PLYN
Vyúčtování za období Variabilní symbol Datum splatnosti Produkt
8. 9. 2023 – 20. 9. 2024 7381440900 9. 10. 2024 Plyn pro ZTP
ČÁST A
ZÁKAZNÍK
Jan Sedláček
Adresa: U vodárny 1059, 503 46 Třebechovice pod Orebem EIC: 27ZG500Z0265207P
Celková spotřeba 20 702,47 kWh
Spotřeba pro určení pásma: 20 586,950 kWh
Spotřeba 11,32081 MWh 1 590,00 Kč 18 000,09 Kč
Spotřeba 1,20250 MWh 1 279,34 Kč 1 538,41 Kč
Stálá platba 8,66700 měs. 105,00 Kč 910,04 Kč
INFORMACE O TRVÁNÍ SMLOUVY
Smlouva je sjednána na dobu neurčitou.
"""
        result = _parse_invoice_text(text, "cez-plyn.pdf")
        self.assertEqual(result["annual_consumption_mwh"], 20.58695)
        self.assertEqual(result["rate_band"], "nad 7,56 do 63 MWh")
        self.assertEqual(result["current_price_vt"], 1279.34)
        self.assertEqual(result["current_monthly_fee"], 105)

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

    def test_ppas_combined_invoice_is_split_into_supply_points(self):
        text = """Fakturační adresa Doručovací adresa
Obec Srch Obec Srch
Pražská plynárenská, a. s. moje.ppas.cz
ODBĚRNÉ MÍSTO:0790298513 TŘÍDA TDD: MO4
ADRESA: Pohránovská 220, 533 52 Srch
EIC KÓD: 27ZG500Z0064115C
PŘEPOČTENÁ ROČNÍ SPOTŘEBA* (v kWh): 20 935
DETAIL SPOTŘEBY:
201719 04.10.2024 - 31.10.2024 01 1 695 1 797 102 1,0261 10,9215 1 143,07 1,14307
201719 01.10.2025 - 06.10.2025 01 3 542 3 575 33 1,0261 10,9215 369,82 0,36982
Celkem: 1 880 21,06832
Způsob odečtu:
01.01.2025 - 06.10.2025 Komoditní složka ceny 13,72802 MWh 0,00000 1 105,00 15 169,46
01.01.2025 - 06.10.2025 Stálý měsíční plat 0,00000 9,19355 0,00 0,00
SOUHRN ZA ODBĚRNÉ MÍSTO:
ODBĚRNÉ MÍSTO:0790298521 TŘÍDA TDD: MO4
ADRESA: Na Kopečku 31/0, 533 52 Srch
EIC KÓD: 27ZG500Z0064120J
PŘEPOČTENÁ ROČNÍ SPOTŘEBA* (v kWh): 100 397
DETAIL SPOTŘEBY:
5747916 04.10.2024 - 31.10.2024 01 114 669 115 159 490 1,0257 10,9215 5 489,07 5,48907
5747916 01.10.2025 - 05.10.2025 01 262 359 97 1,0261 10,9215 1 087,04 1,08704
Celkem: 8 999 100,81000
Způsob odečtu:
01.01.2025 - 05.10.2025 Komoditní složka ceny 64,33569 MWh 0,00000 1 105,00 71 090,93
01.01.2025 - 05.10.2025 Kapacitní složka ceny 79,93600 Nm3 9,16129 0,00 0,00
Datum platnosti smlouvy a produktu ke dni vystavení faktury
3000658584_01 PLYN INDIVIDUAL 31.12.2025 31.12.2025
3000658590_01 PLYN INDIVIDUAL 31.12.2025 31.12.2025
"""
        points = _parse_ppas_points(text, "ppas.pdf")
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["ean_eic"], "27ZG500Z0064115C")
        self.assertEqual(points[0]["annual_consumption_mwh"], 20.935)
        self.assertEqual(points[0]["current_price_vt"], 1105)
        self.assertEqual(points[0]["contract_end_date"], date(2025, 12, 31))
        self.assertEqual(points[1]["annual_consumption_mwh"], 100.397)
        self.assertEqual(points[1]["rate_band"], "nad 63 MWh")

    def test_consecutive_invoices_for_same_ean_are_annualized_together(self):
        base = {
            "ean_eic": "EAN1", "commodity": "Elektřina", "vt_share": 100,
            "warnings": [], "consumption_source": "Skutečná spotřeba za období",
        }
        first = base | {
            "file_name": "leden.pdf", "billing_from": date(2025, 1, 1),
            "billing_to": date(2025, 1, 31), "actual_consumption_mwh": 1,
            "annual_consumption_mwh": 11.774,
        }
        second = base | {
            "file_name": "unor.pdf", "billing_from": date(2025, 2, 1),
            "billing_to": date(2025, 2, 28), "actual_consumption_mwh": 2,
            "annual_consumption_mwh": 26.071,
        }
        result = merge_invoice_supply_points([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["actual_consumption_mwh"], 3)
        self.assertEqual(result[0]["billing_from"], date(2025, 1, 1))
        self.assertAlmostEqual(result[0]["annual_consumption_mwh"], 18.559322, places=6)

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
