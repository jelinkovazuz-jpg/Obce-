import unittest
from datetime import date

import duckdb

from app.energy_calculator import (
    add_supply_point,
    annualize_consumption,
    calculate_supply_point,
    create_quote,
    derive_supply_start,
    EnergyCalculationError,
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

    def test_fixed_contract_starts_day_after_it_ends(self):
        self.assertEqual(
            derive_supply_start(
                "Doba určitá", date(2026, 8, 1),
                contract_end_date=date(2027, 1, 31),
            ),
            date(2027, 2, 1),
        )

    def test_first_year_is_split_across_three_price_periods(self):
        result = calculate_supply_point(self.conn, self.add_point(), 12)
        self.assertEqual(result["innogy_fixed"], 1524)
        self.assertEqual(
            sorted({period["unit_price"] for period in result["periods"]}),
            [2280.0, 2355.0, 2523.2],
        )
        expected_energy = 12 / 365 * (31 * 2355 + 181 * 2280 + 153 * 2523.2)
        self.assertAlmostEqual(result["innogy_energy"], expected_energy, places=2)

    def test_mid_month_start_uses_daily_overlap(self):
        point = self.add_point()
        self.conn.execute(
            "UPDATE energy_supply_points SET supply_start_date=? WHERE id=?",
            [date(2027, 5, 25), point],
        )
        result = calculate_supply_point(self.conn, point, 12)
        self.assertEqual(sum(line["days"] for line in result["lines"] if line["component"] == "VT"), 366)
        prices_by_start = {line["from"]: line["unit_price"] for line in result["lines"]}
        self.assertEqual(prices_by_start[date(2027, 5, 25)], 2280)
        self.assertEqual(prices_by_start[date(2027, 7, 1)], 2523.2)
        self.assertEqual(prices_by_start[date(2028, 1, 1)], 2440.2)

    def test_36_months_charge_exactly_36_fixed_fees(self):
        result = calculate_supply_point(self.conn, self.add_point(), 36)
        self.assertEqual(result["innogy_fixed"], 36 * 127)
        self.assertEqual(result["current_fixed"], 36 * 100)

    def test_zero_consumption_keeps_negative_saving(self):
        result = calculate_supply_point(
            self.conn, self.add_point("Plyn", consumption=0, current_fee=50), 12
        )
        self.assertEqual(result["saving"], -960)

    def test_supply_cannot_start_before_offer_was_prepared(self):
        quote = create_quote(
            self.conn, None, "Testovací obec", "innogy-optimal36-ele",
            "optimal36-ele-example", date(2026, 8, 1), "Test", "tester",
        )
        with self.assertRaises(EnergyCalculationError):
            add_supply_point(
                self.conn, quote, "Adresa", "EAN", "Elektřina", "Všechny",
                1, 100, "Dodavatel", 3000, None, 100, "Doba určitá",
                date(2026, 5, 24), None, None, date(2026, 5, 25),
                "innogy-optimal36-ele", "optimal36-ele-example",
            )

    def test_invoice_consumption_is_annualized_for_short_period(self):
        self.assertEqual(
            annualize_consumption(5, date(2026, 1, 1), date(2026, 6, 30)),
            round(5 / 181 * 365, 6),
        )
        self.assertEqual(
            annualize_consumption(10, date(2026, 1, 1), date(2026, 12, 31)),
            10,
        )
        self.assertEqual(
            annualize_consumption(10, date(2026, 1, 1), date(2026, 6, 30), stated_annual=18),
            18,
        )


if __name__ == "__main__":
    unittest.main()
