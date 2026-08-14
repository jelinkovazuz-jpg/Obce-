import unittest
from datetime import date

import duckdb

from app.energy_calculator import (
    add_supply_point,
    calculate_supply_point,
    create_quote,
    derive_supply_start,
    init_energy_calculator,
)


class EnergyCalculatorTest(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        init_energy_calculator(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_point(self, commodity="Elektřina", consumption=12, current_fee=100):
        product = "innogy-optimal36-ele" if commodity == "Elektřina" else "innogy-optimal36-gas"
        price_list = "optimal36-ele-example" if commodity == "Elektřina" else "optimal36-gas-example"
        rate = "Všechny" if commodity == "Elektřina" else "nad 7,56 do 63 MWh"
        quote = create_quote(
            self.conn, None, "Testovací obec", product, price_list,
            date(2026, 8, 1), "Test", "tester",
        )
        return add_supply_point(
            self.conn, quote, "Radnice 1", f"ID-{commodity}", commodity, rate,
            consumption, 70, "Původní dodavatel", 3000, 2500 if commodity == "Elektřina" else None,
            current_fee, "Doba neurčitá", None, 3, date(2026, 8, 10),
            date(2026, 12, 1), product, price_list,
        )

    def test_notice_submitted_in_august_starts_in_december(self):
        self.assertEqual(
            derive_supply_start(
                "Doba neurčitá", date(2026, 8, 1),
                notice_months=3, notice_submitted_date=date(2026, 8, 10),
            ),
            date(2026, 12, 1),
        )

    def test_first_year_is_split_across_three_price_periods(self):
        result = calculate_supply_point(self.conn, self.add_point(), 12)
        self.assertEqual(result["innogy_fixed"], 1524)
        self.assertEqual(
            sorted({period["unit_price"] for period in result["periods"]}),
            [2280.0, 2355.0, 2523.2],
        )

    def test_36_months_charge_exactly_36_fixed_fees(self):
        result = calculate_supply_point(self.conn, self.add_point(), 36)
        self.assertEqual(result["innogy_fixed"], 36 * 127)
        self.assertEqual(result["current_fixed"], 36 * 100)

    def test_zero_consumption_keeps_negative_saving(self):
        result = calculate_supply_point(
            self.conn, self.add_point("Plyn", consumption=0, current_fee=50), 12
        )
        self.assertEqual(result["saving"], -960)


if __name__ == "__main__":
    unittest.main()
