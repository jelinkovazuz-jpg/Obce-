import unittest
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import duckdb

from app.energy_calculator import init_energy_calculator
from app.energy_price_import import (
    EnergyPriceImportError,
    import_monthly_price_list,
    parse_innogy_price_pdf,
)


CSV = """Komodita;Produkt;Sazba/pásmo;Složka;Cena platí od;Cena platí do;Cena Kč/MWh;Stálý plat Kč/měsíc
Elektřina;Optimal 36;Všechny;VT;01.08.2026;31.12.2026;2355,00;127
Elektřina;Optimal 36;Všechny;NT;01.08.2026;31.12.2026;2355,00;127
Elektřina;Optimal 36;Všechny;VT;01.01.2027;;2440,20;127
Elektřina;Optimal 36;Všechny;NT;01.01.2027;;2440,20;127
"""

PDF_HEADER = """Exkluzivní nabídka
srpen 2026*
{commodity}
Optimal 36
Období dodávky Celé období dodávky do 31. 12. 2026 1. 1. 2027 - 30. 6. 2027 1. 7. 2027 - 31. 12. 2027 1. 1. 2028 - 31. 12. 2028 od 1. 1. 2029
{rows}
Akce platí pro smlouvy uzavřené od 1. 8. 2026 do 31. 8. 2026 výhradně.
"""


class FakePdf:
    def __init__(self, text):
        self.pages = [SimpleNamespace(extract_text=lambda **_: text)]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class EnergyPriceImportTest(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        init_energy_calculator(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_monthly_file_replaces_active_list_but_keeps_history(self):
        result = import_monthly_price_list(
            self.conn, BytesIO(CSV.encode("utf-8")), "akce_srpen.csv",
            date(2026, 8, 14), "admin",
        )
        self.assertEqual(result["rows"], 4)
        active = self.conn.execute("""
            SELECT pl.name,count(pp.id)
            FROM energy_price_lists pl
            JOIN energy_price_periods pp ON pp.price_list_id=pl.id
            WHERE pl.product_id='innogy-optimal36-ele' AND pl.active
            GROUP BY pl.id,pl.name
        """).fetchone()
        self.assertEqual(active[1], 4)
        self.assertIn("08/2026", active[0])
        self.assertGreaterEqual(
            self.conn.execute("""
                SELECT count(*) FROM energy_price_lists
                WHERE product_id='innogy-optimal36-ele'
            """).fetchone()[0],
            2,
        )

    def test_bad_file_is_rejected_without_partial_import(self):
        before = self.conn.execute("SELECT count(*) FROM energy_price_imports").fetchone()[0]
        with self.assertRaises(EnergyPriceImportError):
            import_monthly_price_list(
                self.conn, BytesIO(b"Komodita;Produkt\nPlyn;Optimal 36\n"),
                "bad.csv", date(2026, 8, 1), "admin",
            )
        after = self.conn.execute("SELECT count(*) FROM energy_price_imports").fetchone()[0]
        self.assertEqual(before, after)

    def test_real_innogy_electricity_layout_is_parsed(self):
        rows = """všechny sazby mimo níže uvedené 127,00 153,67 2 355,00 2 849,55 2 280,00 2 758,80 2 523,20 3 053,07 2 440,20 2 952,64 2 440,20 2 952,64
D27d, D56d, D57d, C27d, C56d 127,00 153,67 2 280,00 2 758,80 2 205,00 2 668,05 2 440,20 2 952,64 2 357,20 2 852,21 2 357,20 2 852,21"""
        text = PDF_HEADER.format(commodity="elektřina", rows=rows)
        with patch.dict("sys.modules", {"pdfplumber": SimpleNamespace(open=lambda _: FakePdf(text))}):
            parsed = parse_innogy_price_pdf(b"pdf", "elektrina.pdf")
        self.assertEqual(parsed["signing_from"], date(2026, 8, 1))
        self.assertEqual(len(parsed["rows"]), 60)
        special = [r for r in parsed["rows"] if r["rate"] == "D27d" and r["component"] == "VT"]
        self.assertEqual([r["unit_price"] for r in special], [2280, 2205, 2440.2, 2357.2, 2357.2])

    def test_real_innogy_gas_layout_is_parsed(self):
        rows = """do 1,89 105,00 127,05 962,00 1 164,02 851,50 1 030,32 982,50 1 188,83 862,50 1 043,63 810,00 980,10
nad 1,89 do 7,56 115,00 139,15 949,00 1 148,29 838,50 1 014,59 967,50 1 170,68 847,50 1 025,48 795,00 961,95
nad 7,56 do 63 130,00 157,30 936,00 1 132,56 825,50 998,86 952,50 1 152,53 832,50 1 007,33 780,00 943,80
nad 63 130,00 157,30 929,50 1 124,70 819,00 990,99 945,00 1 143,45 825,00 998,25 772,50 934,73"""
        text = PDF_HEADER.format(commodity="plyn", rows=rows)
        with patch.dict("sys.modules", {"pdfplumber": SimpleNamespace(open=lambda _: FakePdf(text))}):
            parsed = parse_innogy_price_pdf(b"pdf", "plyn.pdf")
        self.assertEqual(len(parsed["rows"]), 20)
        band = [r for r in parsed["rows"] if r["rate"] == "nad 7,56 do 63 MWh"]
        self.assertEqual(band[0]["monthly_fee"], 130)
        self.assertEqual([r["unit_price"] for r in band], [936, 825.5, 952.5, 832.5, 780])


if __name__ == "__main__":
    unittest.main()
