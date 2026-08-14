import unittest
from datetime import date
from io import BytesIO

import duckdb

from app.energy_calculator import init_energy_calculator
from app.energy_price_import import EnergyPriceImportError, import_monthly_price_list


CSV = """Komodita;Produkt;Sazba/pásmo;Složka;Cena platí od;Cena platí do;Cena Kč/MWh;Stálý plat Kč/měsíc
Elektřina;Optimal 36;Všechny;VT;01.08.2026;31.12.2026;2355,00;127
Elektřina;Optimal 36;Všechny;NT;01.08.2026;31.12.2026;2355,00;127
Elektřina;Optimal 36;Všechny;VT;01.01.2027;;2440,20;127
Elektřina;Optimal 36;Všechny;NT;01.01.2027;;2440,20;127
"""


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


if __name__ == "__main__":
    unittest.main()
