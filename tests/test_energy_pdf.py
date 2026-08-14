import unittest
from datetime import date

import duckdb

from app.energy_calculator import (
    add_supply_point, calculate_quote, create_quote, init_energy_calculator,
)
from app.energy_pdf import build_energy_offer_pdf


class EnergyPdfTest(unittest.TestCase):
    def test_offer_contains_summary_and_one_page_per_supply_point(self):
        conn = duckdb.connect(":memory:")
        init_energy_calculator(conn)
        quote = create_quote(
            conn, None, "Městys Choltice", "innogy-optimal36-ele",
            "optimal36-ele-example", date(2026, 8, 13), "Nabídka", "test",
        )
        add_supply_point(
            conn, quote, "Ledec 29", "859182400700227603", "Elektřina",
            "Všechny", 0.797, 100, "ČEZ Prodej", 3475.21, None, 128,
            "Doba určitá", date(2026, 11, 30), None, None,
            date(2026, 12, 1), "innogy-optimal36-ele", "optimal36-ele-example",
        )
        pdf = build_energy_offer_pdf(conn, quote, calculate_quote(conn, quote))
        conn.close()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 20_000)
        # Summary page + one detail page.
        self.assertGreaterEqual(pdf.count(b"/Type /Page"), 2)


if __name__ == "__main__":
    unittest.main()
