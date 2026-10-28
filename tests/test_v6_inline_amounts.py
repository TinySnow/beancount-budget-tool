"""User-facing posting labels must preserve exact booked amounts."""

from fractions import Fraction
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.inline_amounts import parse_inline_amounts


class InlineAmountTests(unittest.TestCase):
    def test_single_name_and_excluded_boundary(self):
        self.assertEqual(parse_inline_amounts("自由资金", Fraction(5000), "virtual",
                                              known_names={"自由资金"}),
                         [("自由资金", Fraction(5000))])
        self.assertEqual(parse_inline_amounts("-", Fraction(-100), "virtual"),
                         [("-", Fraction(-100))])

    def test_mixed_punctuation_and_exact_sum(self):
        raw = "电子产品100, 储蓄200,旅游300｜保险400元|生活费：600元；Other Usage:100;"
        parts = parse_inline_amounts(raw, Fraction(-1700), "virtual")
        self.assertEqual(parts, [
            ("电子产品", Fraction(-100)), ("储蓄", Fraction(-200)),
            ("旅游", Fraction(-300)), ("保险", Fraction(-400)),
            ("生活费", Fraction(-600)), ("Other Usage", Fraction(-100))])

    def test_mismatch_and_negative_slice_are_rejected(self):
        for raw in ("储蓄100，旅游50", "储蓄-100，旅游100"):
            with self.subTest(raw=raw), self.assertRaises(BudgetError):
                parse_inline_amounts(raw, Fraction(200), "virtual",
                                     known_names={"储蓄", "旅游"})

    def test_registered_name_ending_in_digits_is_not_amount(self):
        self.assertEqual(parse_inline_amounts("旅行2026", Fraction(50), "virtual",
                                              known_names={"旅行2026"}),
                         [("旅行2026", Fraction(50))])


if __name__ == "__main__":
    unittest.main()
