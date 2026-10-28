"""v6 首个纯计算切片：安全可花与独立的简单计划。

本模块绝不解析旧 ``budget`` 标签，也不读取 v4 的 P/S/R 结果。
输入是已审阅的状态，而不是从账户名称或金额猜出的状态。尚无 v6
事实编译器时，调用者不能把本模块的合成示例标成正式账本报告。

首轮仅处理一种报告货币、已结算可支付现金、有身份的责任与独占
还款覆盖；复杂 K、证券、外币未锁定清偿及带副作用的计划由后续
协议处理。拒绝返回能力状态，不返回一个看似精确的部分金额。
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from fractions import Fraction
import calendar
from typing import Dict, Optional, Tuple


class Status(str, Enum):
    CERTIFIED = "CERTIFIED"
    UNKNOWN = "UNKNOWN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"


class PlanKind(str, Enum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"


@dataclass(frozen=True)
class Capability:
    """一个度量、范围、时点的签发结果；非认证状态不附带数值。"""

    metric: str
    scope: str
    as_of: date
    status: Status
    value: Optional[Fraction]
    reasons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.status == Status.CERTIFIED) != (self.value is not None):
            raise ValueError("只有 CERTIFIED 能持有数值，且必须持有数值")


@dataclass(frozen=True)
class CashResource:
    """当前真实现金的用途切片；旧模式 owner='-' 为自由现金。

    ``payable`` 不是从 H 或账户名推导的：调用者必须从支付范围声明
    提供。不能支付的真实资产可以另行报告持有，但不得成为首页资源。
    新双默认用途模式可将有来源的负数用途切片传入。负切片不能
    单独提供责任覆盖；受保护用途的净负份额会占用自由池。
    """

    id: str
    amount: Fraction
    currency: str
    owner: str
    payable: bool
    evidence: str


@dataclass(frozen=True)
class Obligation:
    """同一责任只有一个 ID；账单到期切片和后续计提不得复制它。

    ``kind`` 仅有 consumer / nonconsumer 两类。前者未偿全额占用；
    后者仅在本月到期或此前逾期时占用。已确定但未 booked 的利息可
    作为独立责任输入，日后 booked 后仍沿用 ID。外币必须带本时点
    锁定的报告币种清偿额，历史费用汇率不能用于此处。
    """

    id: str
    remaining: Fraction
    currency: str
    kind: str
    owner: str
    due_on: Optional[date]
    evidence: str
    settlement_amount: Optional[Fraction] = None
    settlement_as_of: Optional[date] = None


@dataclass(frozen=True)
class Coverage:
    """现存支付资源与责任之间的独占分配，不创造现金或新的责任。"""

    id: str
    resource_id: str
    obligation_id: str
    amount: Fraction
    evidence: str


@dataclass(frozen=True)
class ActualInput:
    as_of: date
    report_currency: str
    resources: Tuple[CashResource, ...]
    obligations: Tuple[Obligation, ...]
    coverage: Tuple[Coverage, ...]
    scope_complete: bool
    scope_evidence: str
    free_owner_ids: Tuple[str, ...] = ("-",)


@dataclass(frozen=True)
class ActualReport:
    capability: Capability
    # 只有签发成功才给明细。调用方不能从失败结果中捞出部分数值。
    free_cash: Optional[Fraction] = None
    uncovered: Optional[Fraction] = None
    purpose_available: Tuple[Tuple[str, Fraction], ...] = ()
    protected_shortfall: Optional[Fraction] = None


@dataclass(frozen=True)
class Realization:
    amount: Fraction
    event_ref: str
    event_capacity: Fraction
    source_id: str


@dataclass(frozen=True)
class Cancellation:
    amount: Fraction
    decision_ref: str


@dataclass(frozen=True)
class SimplePlan:
    """首版的无副作用计划，而非一般金融模拟器。

    ``source_capacity`` 是该稳定来源可分给所有计划的总额；多个
    计划共源时联合检查容量。兑现只消耗计划余额，实际现金必须由
    同一真实财务事实另行编译，绝不在这里再加一次。

    ``remaining_outside_actual`` 断言未兑现的补给尚未进入实际可花；
    这必须有来源证明。解除债务预留、卖出未结算证券等非独立净增量
    不属于 simple plan，不能把 ``simple_net`` 随意设成 True。
    """

    id: str
    kind: PlanKind
    amount: Fraction
    source_id: str
    source_capacity: Fraction
    effective_on: date
    source_kind: str  # future_cash for inflow; free_cash for outflow
    remaining_outside_actual: bool
    simple_net: bool
    evidence: str
    realizations: Tuple[Realization, ...] = ()
    cancellations: Tuple[Cancellation, ...] = ()


def _cap(metric: str, state: ActualInput, status: Status,
         value: Optional[Fraction] = None, *reasons: str) -> Capability:
    return Capability(metric, f"self/{state.report_currency}/direct_payment",
                      state.as_of, status, value, tuple(reasons))


def _fail(state: ActualInput, status: Status, reason: str) -> ActualReport:
    return ActualReport(_cap("actual_spendable", state, status, None, reason))


def _unique_nonempty(values, field: str) -> bool:
    ids = [getattr(item, field) for item in values]
    return all(isinstance(ident, str) and bool(ident.strip()) for ident in ids) and len(ids) == len(set(ids))


def _amount(value: object, *, allow_zero: bool = True) -> bool:
    # bool 是 int 的子类；拒绝它可避免 True 悄悄成为 1 元。
    return isinstance(value, (int, Fraction)) and not isinstance(value, bool) and (
        value >= 0 if allow_zero else value > 0)


def _settlement(obligation: Obligation, state: ActualInput) -> Optional[Fraction]:
    if obligation.currency == state.report_currency and obligation.settlement_amount is None:
        return obligation.remaining
    if (obligation.settlement_amount is None or
            obligation.settlement_as_of != state.as_of):
        return None
    return obligation.settlement_amount


def calculate_actual(state: ActualInput) -> ActualReport:
    """按责任 ID 取并集、独占覆盖后计算首页安全可花。

    负值仍是签发结果，表示已超支；未知范围、清偿价或覆盖关系不
    得被填成 0。此函数不从财务账推断账户是否完整，那是编译层的
    举证责任，``scope_complete`` 和 ``scope_evidence`` 必须提供。
    """
    if not isinstance(state.as_of, date) or not state.report_currency:
        return _fail(state, Status.INVALID, "TIME_INVALID")
    if (not isinstance(state.free_owner_ids, tuple) or not state.free_owner_ids or
            any(not isinstance(owner, str) or not owner for owner in state.free_owner_ids) or
            len(state.free_owner_ids) != len(set(state.free_owner_ids))):
        return _fail(state, Status.INVALID, "FREE_OWNER_IDS_INVALID")
    if not all(_unique_nonempty(rows, "id") for rows in
               (state.resources, state.obligations, state.coverage)):
        return _fail(state, Status.INVALID, "DUPLICATE_OR_MISSING_ID")
    if any(not row.evidence for row in (*state.resources, *state.obligations, *state.coverage)):
        return _fail(state, Status.NEEDS_REVIEW, "EVIDENCE_MISSING")

    resources = {row.id: row for row in state.resources}
    obligations = {row.id: row for row in state.obligations}
    if any((not isinstance(row.amount, (int, Fraction)) or
            isinstance(row.amount, bool)) or
           row.currency != state.report_currency or
           not row.owner for row in state.resources):
        return _fail(state, Status.INVALID, "RESOURCE_INVALID")
    if any(not _amount(row.remaining) or row.kind not in ("consumer", "nonconsumer") or
           not row.owner or not row.currency or
           (row.kind == "nonconsumer" and not isinstance(row.due_on, date)) or
           (row.due_on is not None and not isinstance(row.due_on, date)) or
           (row.settlement_amount is not None and not _amount(row.settlement_amount))
           for row in state.obligations):
        return _fail(state, Status.INVALID, "OBLIGATION_INVALID")
    if any(row.remaining == 0 and row.settlement_amount not in (None, 0)
           for row in state.obligations):
        return _fail(state, Status.INVALID, "OBLIGATION_INVALID")

    # 本月非消费责任的到期窗口按 as_of 所在自然月确定；更早未清逾期
    # 同样落在这个集合里。消费责任不因分期或账单日在未来而消失。
    month_end = date(state.as_of.year, state.as_of.month,
                     calendar.monthrange(state.as_of.year, state.as_of.month)[1])
    included = {ident for ident, row in obligations.items()
                if row.kind == "consumer" or row.due_on <= month_end}
    values: Dict[str, Fraction] = {}
    for ident, row in obligations.items():
        value = _settlement(row, state)
        if ident in included and value is None:
            return _fail(state, Status.NEEDS_REVIEW, "SETTLEMENT_VALUE_MISSING")
        if value is not None:
            values[ident] = value

    resource_used: Dict[str, Fraction] = {ident: Fraction(0) for ident in resources}
    obligation_covered: Dict[str, Fraction] = {ident: Fraction(0) for ident in obligations}
    for row in state.coverage:
        if not _amount(row.amount, allow_zero=False) or row.resource_id not in resources or row.obligation_id not in obligations:
            return _fail(state, Status.INVALID, "COVERAGE_INVALID")
        resource = resources[row.resource_id]
        if not resource.payable or resource.amount <= 0:
            return _fail(state, Status.INVALID, "COVERAGE_RESOURCE_INVALID")
        resource_used[row.resource_id] += row.amount
        obligation_covered[row.obligation_id] += row.amount
    if any(resource_used[ident] > max(row.amount, Fraction(0))
           for ident, row in resources.items()):
        return _fail(state, Status.INVALID, "COVERAGE_RESOURCE_OVERALLOCATED")
    for ident, covered in obligation_covered.items():
        # 覆盖未来非消费责任也允许，但若它是外币且缺当前清偿价，
        # 不能用原生数值与报告币种现金比较。
        if covered and ident not in values:
            return _fail(state, Status.NEEDS_REVIEW, "SETTLEMENT_VALUE_MISSING")
        if covered > values.get(ident, Fraction(0)):
            return _fail(state, Status.INVALID, "COVERAGE_OBLIGATION_OVERALLOCATED")

    if not state.scope_complete or not state.scope_evidence:
        return _fail(state, Status.NEEDS_REVIEW, "SCOPE_PENDING")
    if any(not row.payable for row in resources.values()):
        # 非支付资产属于另一度量，不应偷偷进入本函数并在求和时忽略。
        return _fail(state, Status.INVALID, "RESOURCE_OUT_OF_SCOPE")

    free_cash = sum((row.amount - resource_used[row.id] for row in resources.values()
                     if row.owner in state.free_owner_ids), Fraction(0))
    uncovered = sum((values[ident] - obligation_covered[ident]
                     for ident in included), Fraction(0))
    purpose_cash: Dict[str, Fraction] = {}
    for row in resources.values():
        if row.owner not in state.free_owner_ids:
            purpose_cash[row.owner] = purpose_cash.get(row.owner, Fraction(0)) + row.amount - resource_used[row.id]
    protected_shortfall = sum((-min(amount, Fraction(0)) for amount in purpose_cash.values()),
                              Fraction(0))
    for ident in included:
        row = obligations[ident]
        if row.owner not in state.free_owner_ids:
            purpose_cash[row.owner] = purpose_cash.get(row.owner, Fraction(0)) - (
                values[ident] - obligation_covered[ident])
    return ActualReport(_cap("actual_spendable", state, Status.CERTIFIED,
                             free_cash - uncovered - protected_shortfall),
                        free_cash, uncovered, tuple(sorted(purpose_cash.items())),
                        protected_shortfall)


def forecast_simple(state: ActualInput, actual: ActualReport,
                    plans: Tuple[SimplePlan, ...], horizon: date,
                    *, plan_set_complete: bool, plan_set_evidence: str) -> Capability:
    """首版仅模拟有证明的独立净流入/流出；复杂副作用明确拒绝。

    兑现切片只减少计划剩额。真实入账必须已体现在 ``actual`` 的
    资源状态中，不能在这里第二次加现金。计划来源联合容量按全部
    有效承诺检查，不能逐计划看都不超额就放过整体超额。
    """
    metric = "planned_spendable"
    if actual.capability.status != Status.CERTIFIED:
        return _cap(metric, state, actual.capability.status, None,
                    *actual.capability.reasons)
    if not plan_set_complete or not plan_set_evidence:
        return _cap(metric, state, Status.NEEDS_REVIEW, None, "PLAN_SET_PENDING")
    if not isinstance(horizon, date) or horizon < state.as_of:
        return _cap(metric, state, Status.INVALID, None, "SCENARIO_HORIZON_INVALID")
    if not _unique_nonempty(plans, "id"):
        return _cap(metric, state, Status.INVALID, None, "PLAN_ID_INVALID")

    commitments: Dict[str, Fraction] = {}
    capacities: Dict[str, Fraction] = {}
    source_kinds: Dict[str, str] = {}
    event_claims: Dict[str, Fraction] = {}
    event_capacities: Dict[str, Fraction] = {}
    event_sources: Dict[str, str] = {}
    delta = Fraction(0)
    for plan in plans:
        if (not isinstance(plan.kind, PlanKind) or not _amount(plan.amount) or
                not _amount(plan.source_capacity) or not plan.source_id or
                not plan.evidence or not isinstance(plan.effective_on, date)):
            return _cap(metric, state, Status.INVALID, None, "PLAN_INVALID")
        if (plan.kind == PlanKind.INFLOW and plan.source_kind != "future_cash") or (
                plan.kind == PlanKind.OUTFLOW and plan.source_kind != "free_cash"):
            return _cap(metric, state, Status.UNSUPPORTED, None, "PLAN_SOURCE_UNSUPPORTED")
        if not plan.simple_net:
            return _cap(metric, state, Status.UNSUPPORTED, None, "PLAN_SIDE_EFFECT_UNSUPPORTED")
        if any(not _amount(item.amount, allow_zero=False) or not item.event_ref or
               not _amount(item.event_capacity, allow_zero=False) or
               item.source_id != plan.source_id
               for item in plan.realizations) or any(
                not _amount(item.amount, allow_zero=False) or not item.decision_ref
                for item in plan.cancellations):
            return _cap(metric, state, Status.INVALID, None, "PLAN_SLICE_INVALID")
        realized = sum((item.amount for item in plan.realizations), Fraction(0))
        cancelled = sum((item.amount for item in plan.cancellations), Fraction(0))
        for item in plan.realizations:
            if (item.event_ref in event_capacities and
                    (event_capacities[item.event_ref] != item.event_capacity or
                     event_sources[item.event_ref] != item.source_id)):
                return _cap(metric, state, Status.INVALID, None, "PLAN_EVENT_CONFLICT")
            event_capacities[item.event_ref] = item.event_capacity
            event_sources[item.event_ref] = item.source_id
            event_claims[item.event_ref] = event_claims.get(item.event_ref, Fraction(0)) + item.amount
        if realized + cancelled > plan.amount:
            return _cap(metric, state, Status.INVALID, None, "PLAN_SLICE_OVERALLOCATED")
        remaining = plan.amount - realized - cancelled
        if plan.source_id in capacities and capacities[plan.source_id] != plan.source_capacity:
            return _cap(metric, state, Status.INVALID, None, "PLAN_SOURCE_CAPACITY_CONFLICT")
        if plan.source_id in source_kinds and source_kinds[plan.source_id] != plan.source_kind:
            return _cap(metric, state, Status.INVALID, None, "PLAN_SOURCE_KIND_CONFLICT")
        capacities[plan.source_id] = plan.source_capacity
        source_kinds[plan.source_id] = plan.source_kind
        commitments[plan.source_id] = commitments.get(plan.source_id, Fraction(0)) + plan.amount - cancelled
        if remaining == 0 or plan.effective_on > horizon:
            continue
        if plan.effective_on <= state.as_of:
            return _cap(metric, state, Status.NEEDS_REVIEW, None, "PLAN_OVERDUE_NEEDS_REVISION")
        if plan.kind == PlanKind.INFLOW and not plan.remaining_outside_actual:
            return _cap(metric, state, Status.NEEDS_REVIEW, None, "PLAN_ALREADY_IN_ACTUAL")
        delta += remaining if plan.kind == PlanKind.INFLOW else -remaining
    if any(committed > capacities[ident] for ident, committed in commitments.items()):
        return _cap(metric, state, Status.INVALID, None, "PLAN_SOURCE_OVERALLOCATED")
    if any(claimed > event_capacities[ident] for ident, claimed in event_claims.items()):
        return _cap(metric, state, Status.INVALID, None, "PLAN_EVENT_OVERALLOCATED")
    return _cap(metric, state, Status.CERTIFIED, actual.capability.value + delta)
