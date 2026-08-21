import unittest
from datetime import date

from src.dates import compute_age


class ComputeAgeTest(unittest.TestCase):
    def test_exact_years_no_months(self):
        self.assertEqual(compute_age("2023-06-15", on=date(2026, 6, 15)), (3, 0))

    def test_years_and_months(self):
        self.assertEqual(compute_age("2023-06-15", on=date(2026, 8, 20)), (3, 2))

    def test_day_of_month_not_yet_reached_rounds_down(self):
        # Birthday is the 15th; on the 10th, that month hasn't counted yet.
        self.assertEqual(compute_age("2023-06-15", on=date(2026, 8, 10)), (3, 1))

    def test_newborn_is_zero_zero(self):
        self.assertEqual(compute_age("2026-08-20", on=date(2026, 8, 20)), (0, 0))

    def test_defaults_to_today(self):
        years, months = compute_age(date.today().isoformat())
        self.assertEqual((years, months), (0, 0))


if __name__ == "__main__":
    unittest.main()
