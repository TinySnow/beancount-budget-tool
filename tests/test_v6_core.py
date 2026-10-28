"""v6 独立金额内核的反例测试，不借用 v4 的 R 或 Rust 旧预算结果。"""

from dataclasses import replace
from datetime import date
from fractions import Fraction
import unittest

from beancount_budget_tool.v6 import (
    ActualInput, Cancellation, CashResource, Coverage, Obligation, PlanKind,
    Realization, SimplePlan, Status, calculate_actual, forecast_simple,
)


DAY = date(2026, 10, 10)
HORIZON = date(2026, 10, 31)


def cash(ident, amount, owner="-"):
    return CashResource(ident, Fraction(amount), "CNY", owner, True, f"审阅:{ident}")


def debt(ident, amount, owner="-", kind="consumer", due=None):
    return Obligation(ident, Fraction(amount), "CNY", kind, owner, due,
                      f"账单:{ident}")


def state(resources=(), obligations=(), coverage=(), complete=True):
    return ActualInput(DAY, "CNY", tuple(resources), tuple(obligations),
                       tuple(coverage), complete, "全部支付账户及责任已审" if complete else "")


def plan(ident="p", amount=1000, source="salary", capacity=1000,
         kind=PlanKind.INFLOW, **kwargs):
    return SimplePlan(ident, kind, Fraction(amount), source, Fraction(capacity),
                      date(2026, 10, 20),
                      "future_cash" if kind == PlanKind.INFLOW else "free_cash",
                      True, True, f"计划:{ident}", **kwargs)


def forecast(input_state, plans):
    return forecast_simple(input_state, calculate_actual(input_state), tuple(plans),
                           HORIZON, plan_set_complete=True,
                           plan_set_evidence="十月计划已审阅")


class ActualSpendableTests(unittest.TestCase):
    def test_two_named_spendable_owners_allow_expense_deficit(self):
        candidate = replace(
            state((cash("income-pool", 2300, "free"),
                   cash("living-pool", -100, "living"),
                   cash("reserve", 700, "savings"))),
            free_owner_ids=("free", "living"))
        report = calculate_actual(candidate)
        self.assertEqual(report.capability.status, Status.CERTIFIED)
        self.assertEqual(report.capability.value, Fraction(2200))
        self.assertEqual(dict(report.purpose_available)["savings"], Fraction(700))
        invalid = replace(candidate, coverage=(Coverage(
            "bad", "living-pool", "card", Fraction(1), "不能用负份额覆盖"),),
            obligations=(debt("card", 1),))
        self.assertEqual(calculate_actual(invalid).capability.status, Status.INVALID)

    def test_negative_protected_share_reduces_actual_only_after_owner_netting(self):
        candidate = replace(
            state((cash("daily", 1000, "free"),
                   cash("travel-a", 100, "travel"),
                   cash("travel-b", -150, "travel"))),
            free_owner_ids=("free", "living"))
        report = calculate_actual(candidate)
        self.assertEqual(report.protected_shortfall, Fraction(50))
        self.assertEqual(report.capability.value, Fraction(950))
        covered = replace(candidate,
                          obligations=(debt("card", 80, "travel"),),
                          coverage=(Coverage("cover", "travel-a", "card",
                                             Fraction(80), "专项还款"),))
        # 还款覆盖占用受保护现金；剩余用途净缺口为 130。
        self.assertEqual(calculate_actual(covered).protected_shortfall, Fraction(130))
        self.assertEqual(calculate_actual(covered).capability.value, Fraction(870))

    def test_5000_4000_card_300_and_repayment_stays_700(self):
        purchase = state((cash("daily", 1000), cash("invest", 4000, "投资")),
                         (debt("card", 300),))
        report = calculate_actual(purchase)
        self.assertEqual(report.capability.status, Status.CERTIFIED)
        self.assertEqual(report.capability.value, Fraction(700))
        self.assertEqual(report.free_cash, Fraction(1000))
        self.assertEqual(report.uncovered, Fraction(300))
        repaid = state((cash("daily", 700), cash("invest", 4000, "投资")), ())
        self.assertEqual(calculate_actual(repaid).capability.value, Fraction(700))

    def test_bill_reference_cannot_create_second_obligation(self):
        original = debt("card-1", 300)
        duplicated = state((cash("daily", 1000),), (original, original))
        result = calculate_actual(duplicated).capability
        self.assertEqual(result.status, Status.INVALID)
        self.assertIsNone(result.value)

    def test_coverage_reservation_and_release_do_not_create_cash(self):
        base = state((cash("daily", 1000),), (debt("card", 300),))
        reserved = replace(base, coverage=(Coverage("hold", "daily", "card", Fraction(300), "还款预留"),))
        for candidate in (base, reserved):
            self.assertEqual(calculate_actual(candidate).capability.value, Fraction(700))
        self.assertEqual(calculate_actual(reserved).free_cash, Fraction(700))
        self.assertEqual(calculate_actual(reserved).uncovered, Fraction(0))

    def test_travel_shortfall_reduces_homepage_without_transfer(self):
        travel = state((cash("daily", 1000), cash("travel", 100, "旅行")),
                       (debt("travel-card", 300, "旅行"),),
                       (Coverage("travel-hold", "travel", "travel-card", Fraction(100), "专项还款"),))
        report = calculate_actual(travel)
        self.assertEqual(report.capability.value, Fraction(800))
        self.assertEqual(dict(report.purpose_available)["旅行"], Fraction(-200))
        self.assertEqual(report.uncovered, Fraction(200))

    def test_travel_83_is_17_only_with_explicit_cover(self):
        travel = state((cash("daily", 1000), cash("travel", 100, "旅行")),
                       (debt("travel-card", 83, "旅行"),),
                       (Coverage("cover", "travel", "travel-card", Fraction(83), "专项还款"),))
        report = calculate_actual(travel)
        self.assertEqual(report.capability.value, Fraction(1000))
        self.assertEqual(dict(report.purpose_available)["旅行"], Fraction(17))
        # 同名用途但缺少覆盖记录，不能假装日常现金不受偿付压力影响。
        without_cover = calculate_actual(replace(travel, coverage=())).capability
        self.assertEqual(without_cover.value, Fraction(917))

    def test_overallocated_cover_is_invalid_not_clamped(self):
        candidate = state((cash("daily", 100),), (debt("card", 300),),
                          (Coverage("hold", "daily", "card", Fraction(200), "错误预留"),))
        result = calculate_actual(candidate).capability
        self.assertEqual(result.status, Status.INVALID)
        self.assertIsNone(result.value)

    def test_consumer_full_balance_and_nonconsumer_month_horizon(self):
        obligations = (debt("installment-card", 1200, due=date(2027, 1, 1)),
                       debt("oct-loan", 300, kind="nonconsumer", due=HORIZON),
                       debt("sep-overdue", 200, kind="nonconsumer", due=date(2026, 9, 30)),
                       debt("nov-loan", 300, kind="nonconsumer", due=date(2026, 11, 30)))
        report = calculate_actual(state((cash("daily", 2000),), obligations))
        self.assertEqual(report.capability.value, Fraction(300))

    def test_bill_confirmed_interest_then_booked_uses_same_id(self):
        original = debt("oct-interest", 50, kind="nonconsumer", due=HORIZON)
        before = state((cash("daily", 1000),), (original,))
        after = state((cash("daily", 1000),),
                      (replace(original, evidence="booked:同一责任ID"),))
        self.assertEqual(calculate_actual(before).capability.value, Fraction(950))
        self.assertEqual(calculate_actual(after).capability.value, Fraction(950))

    def test_foreign_debt_needs_current_settlement_not_historical_cost(self):
        foreign = Obligation("usd-card", Fraction(100), "USD", "consumer", "-",
                             date(2026, 11, 10), "美元卡账单")
        missing = calculate_actual(state((cash("daily", 1000),), (foreign,))).capability
        self.assertEqual(missing.status, Status.NEEDS_REVIEW)
        self.assertIsNone(missing.value)
        locked = replace(foreign, settlement_amount=Fraction(800), settlement_as_of=DAY)
        self.assertEqual(calculate_actual(state((cash("daily", 1000),), (locked,))).capability.value,
                         Fraction(200))

    def test_negative_is_certified_but_incomplete_scope_is_not(self):
        complete = state((cash("daily", 100),), (debt("card", 300),))
        negative = calculate_actual(complete).capability
        self.assertEqual(negative.status, Status.CERTIFIED)
        self.assertEqual(negative.value, Fraction(-200))
        partial = calculate_actual(replace(complete, scope_complete=False,
                                           scope_evidence="")).capability
        self.assertEqual(partial.status, Status.NEEDS_REVIEW)
        self.assertIsNone(partial.value)


class SimplePlanTests(unittest.TestCase):
    def test_700_actual_1700_planned_and_partial_realization(self):
        initial = state((cash("daily", 1000), cash("invest", 4000, "投资")),
                        (debt("card", 300),))
        self.assertEqual(forecast(initial, (plan(),)).value, Fraction(1700))
        after = state((cash("daily", 1100), cash("invest", 4000, "投资")), ())
        partial = plan(realizations=(Realization(Fraction(400), "工资入账-1",
                                                 Fraction(1000), "salary"),))
        self.assertEqual(calculate_actual(after).capability.value, Fraction(1100))
        self.assertEqual(forecast(after, (partial,)).value, Fraction(1700))

    def test_outflow_is_in_scenario_and_goal_is_not_a_plan(self):
        current = state((cash("daily", 1000),), (debt("card", 300),))
        inflow = plan()
        outflow = plan("future-transfer", 500, "free-source", 500, PlanKind.OUTFLOW)
        self.assertEqual(forecast(current, (inflow, outflow)).value, Fraction(1200))
        self.assertEqual(forecast(current, ()).value, Fraction(700))

    def test_missing_plan_set_or_already_counted_source_has_no_number(self):
        current = state((cash("daily", 700),))
        actual = calculate_actual(current)
        missing = forecast_simple(current, actual, (), HORIZON,
                                  plan_set_complete=False, plan_set_evidence="")
        self.assertEqual(missing.status, Status.NEEDS_REVIEW)
        self.assertIsNone(missing.value)
        duplicate = forecast(current, (replace(plan(), remaining_outside_actual=False),))
        self.assertEqual(duplicate.status, Status.NEEDS_REVIEW)
        self.assertIsNone(duplicate.value)

    def test_two_plans_overdraw_one_source(self):
        current = state((cash("daily", 0),))
        first = plan("a", 800, "salary", 1000)
        second = plan("b", 800, "salary", 1000)
        result = forecast(current, (first, second))
        self.assertEqual(result.status, Status.INVALID)
        self.assertIn("PLAN_SOURCE_OVERALLOCATED", result.reasons)

    def test_same_source_cannot_be_future_income_and_free_cash(self):
        current = state((cash("daily", 1000),))
        first = plan("income", 500, "same", 1000)
        second = plan("outflow", 500, "same", 1000, PlanKind.OUTFLOW)
        result = forecast(current, (first, second))
        self.assertEqual(result.status, Status.INVALID)
        self.assertIn("PLAN_SOURCE_KIND_CONFLICT", result.reasons)

    def test_one_income_event_can_fund_disjoint_plan_slices(self):
        current = state((cash("daily", 2000),))
        first = plan("a", 800, "salary", 2000,
                     realizations=(Realization(Fraction(800), "income-1", Fraction(2000), "salary"),))
        second = plan("b", 1200, "salary", 2000,
                      realizations=(Realization(Fraction(1200), "income-1", Fraction(2000), "salary"),))
        self.assertEqual(forecast(current, (first, second)).value, Fraction(2000))
        duplicate = replace(second, realizations=(Realization(Fraction(1200), "income-1",
                                                              Fraction(1000), "salary"),))
        self.assertEqual(forecast(current, (first, duplicate)).status, Status.INVALID)

    def test_cancellation_keeps_realized_history(self):
        current = state((cash("daily", 400),))
        closed = plan(realizations=(Realization(Fraction(400), "income-1",
                                             Fraction(1000), "salary"),),
                      cancellations=(Cancellation(Fraction(600), "cancel-1"),))
        self.assertEqual(forecast(current, (closed,)).value, Fraction(400))

    def test_protected_release_with_debt_effect_is_not_simple_cash(self):
        current = state((cash("daily", 0), cash("reserved", 1000, "还债")),
                        (debt("card", 1000),),
                        (Coverage("cover", "reserved", "card", Fraction(1000), "还款"),))
        release = replace(plan(), source_kind="protected_release", simple_net=False)
        result = forecast(current, (release,))
        self.assertEqual(result.status, Status.UNSUPPORTED)
        self.assertIsNone(result.value)


if __name__ == "__main__":
    unittest.main()
