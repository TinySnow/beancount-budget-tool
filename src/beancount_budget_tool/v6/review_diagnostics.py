"""Readable review reasons and source positions shared by monthly diagnostics."""

from __future__ import annotations

from beancount.core import data

from .classification import _key
from .facts import FactIndex


MESSAGES = {
    "LIABILITY_CREDIT_RECEIPT_PURPOSE_MISMATCH": "预付收回与个人收入合并到账时，原预付款仍须恢复到已证明的 fund 用途；请核对收款资产腿的用途切片，个人收入不能掩盖本金改用途。",
    "LIABILITY_BUDGET_AMBIGUOUS": "该负债账户有多个责任用途，或此前用途尚未确定；本次无法唯一分配。请在这条 Liabilities 分录填写 budget（可写多用途及金额），不用重复填逐笔审阅理由。",
    "LIABILITY_BUDGET_TRANSFER_MISMATCH": "债务转移两侧的责任用途金额不一致；请核对各条 Liabilities 的 budget，债务搬移不能暗中改用途。",
    "H_REDEMPTION_OWNER_PENDING": "理财赎回本金与实收现金的 fund 尚未完整确定，或声明了不适用的划拨角色；先确认两条资产腿的持有用途。",
    "H_REDEMPTION_PURPOSE_MISMATCH": "理财赎回本金与实收现金的用途名称不一致；本金与损益按同名用途核对，改用途请另记明确的虚拟调拨。",
    "LIABILITY_CREDIT_PURPOSE_PENDING": "本次是预付／溢缴余额退回，原资金用途未能唯一确定；在收款资产腿明确 fund，不能把它当成新收入或自动挑一份用途。",
    "LIABILITY_CREDIT_MIXED_UNSUPPORTED": "该复合收款不满足已支持的单现金、单负债原预付收回＋Income 等式，可能含新增借款、多现金／多负债或其他清算；仍需联合流量协议，不能靠批准绕过。",
    "LIABILITY_CREDIT_GROUP_UNSUPPORTED": "该退回涉及多账户混合预付的用途分配或后续来源约束，程序尚不能完整证明；不能凭收款 fund 覆盖旧来源或要求人工批准绕过。",
    "LIABILITY_RULE_BALANCE_INVALID": "负债账户规则与余额不符：应收账户不能自动形成应付款，带成本批次也不能按普通现金负债清算；核对账户 role、原分录或拆分往来对象。",
    "LIABILITY_RULE_OWNER_CONFLICT": "当前负债责任用途与已有逐笔审阅不同；请核对负债 budget 和原审阅，不能自动覆盖不同用途。",
    "LIABILITY_RULE_PURPOSE_MISMATCH": "该应收责任的用途与实际付款／收款 fund 不一致，不能自动把报销划给另一用途；请核对负债 budget、资产 fund 或明确拆分往来。",
    "DEFAULT_CASH_SPLIT_AMBIGUOUS": "同笔付款包含本金／利息或不同清算类型，且现金使用多份资金；不能猜每份资金支付哪部分，请明确拆分付款。",
    "DEFAULT_OUTFLOW_EVENT_UNSUPPORTED": "默认支出用途参与了复合收付，当前协议不能唯一分开消费、还债和资产搬移；需要补充流向编译协议。",
    "ACCOUNT_UNREVIEWED": "该资产账户尚未声明为 H 真实库存或 K 申赎累计账户。",
    "PAYMENT_SCOPE_PENDING": "该账户的此币种能否直接支付尚未确定；审定 payable 或 excluded。",
    "CLASSIFICATION_UNREVIEWED": "这条收入或费用缺少行内 budget、侧文件认领或已审默认分类。",
    "OWNERSHIP_UNREVIEWED": "这条资产分录缺少 fund、侧文件用途认领或可确定的默认用途。",
    "PAD_RECONCILIATION_REVIEW": "这笔 pad 是已有余额的校正，或不符合首次正数资产／Equity 初始化条件；请确认差额原因及资金用途，不能再次按期初自由资金自动处理。",
    "FUND_MIXED_SOURCE": "转出账户同时持有多种资金用途，这笔部分转出无法唯一确定来源；在转出分录填写 fund，或明确该账户的转出用途顺序。",
    "FUND_SOURCE_OPENING_PENDING": "转出账户的期初用途尚未完整审定，不能自动继承转出资金用途；先核对 opening 中该账户的用途拆分。",
    "FUND_SOURCE_INSUFFICIENT": "已审用途库存或配置的转出用途顺序不足以覆盖本次转出；核对转出金额、此前资金来源及 fund，不会自动挪用其他用途。",
    "FUND_SOURCE_UNRESOLVED": "转入用途依赖尚未审定的转出资金；先处理同笔交易转出分录的用途问题，再重新计算。",
    "POOL_BOUNDARY_PENDING": "这笔资金跨越证券池边界，需要确认现金转入或转出的用途。",
    "H_TRANSFER_PENDING": "这笔资产转账尚未完成用途审阅。",
    "H_TRANSFER_OWNER_PENDING": "资产转账的一侧或两侧资金用途尚未认领。",
    "H_TRANSFER_MIXED_REMAP_PENDING": "跨账户两侧均为多用途且金额不对应，或转账混有费用／退款而用途变更未明确；核对 fund，改用途须另记同账户虚拟调拨。单用途拆分与多用途合并为单用途可直接标注。",
    "H_TRANSFER_SHAPE_UNSUPPORTED": "该复合资产转账不符合当前简单转账协议，需补程序实现。",
    "H_FX_SHAPE_UNSUPPORTED": "该复合购汇交易不符合当前简单购汇协议，需补程序实现。",
    "H_FX_PENDING": "购汇用途尚不能由账本唯一证明。核对同笔交易的两条 H 资产分录：转入腿须保留与扣款精确相等的原文 @@ 报告货币总价，两腿须分别写明同一个单用途 fund。缺少或冲突时请修正账本；含费用、多用途或其他复杂形态暂不自动认证，无需新增 h_fx 配置。",
    "PAYABLE_CASH_PENDING": "可直接支付账户的用途库存尚未审定；核对期初用途、分录归属或支付范围。",
    "BOOKED_OBLIGATIONS_PENDING": "已入账责任尚未审定；核对消费责任、还款和代垫清算的来源。",
    "BOOKED_LIABILITY_UNMATCHED": "该账户的账本余额与已审代垫应收减去本人未偿责任不一致；核对所有消费、还款、代垫和报销来源。",
    "SETTLEMENT_VALUE_MISSING": "外币责任缺少报告日清偿决定；账单已锁定报告货币金额时填 debt_settlements，尚未锁定时明确声明 unlocked。",
    "SETTLEMENT_PATH_UNSUPPORTED": "这笔外币责任尚未锁定报告货币金额；可执行兑换路径及费用的报告日计价仍需继续实现，不能拿历史消费价或未来还款价代替。",
    "OBLIGATION_PRICE_MISMATCH": "消费与负债两腿的原始价格权重不相等；核对该交易计价残差，不能通过 loader 容差放行。",
    "DEBT_COVERAGE_PENDING": "专项责任缺少覆盖决定；在 debt_coverage 中逐笔声明专用支付资源，或明确 resources: [] 表示没有专用覆盖。",
    "EXTERNAL_SCOPE_PENDING": "缺少本月末的完整性声明；核对截至该日已发生但漏记的责任或在途付款，再提供 completeness。",
    "CLASSIFICATION_PENDING": "这条收入或费用尚未确定预算类别。",
    "VALUATION_MISSING": "这条外币消费缺少有来源的报告货币计价或已审核的实际还款成本。",
    "BENEFIT_SOURCE_PENDING": "退款或收入的经济利益来源尚未审定。",
    "BENEFIT_GROUP_REQUIRED": "这笔多分录退款或收入需要审阅来源分组，防止本金与收益重复计入。",
    "BENEFIT_GROUP_SHAPE_UNSUPPORTED": "该复合退款或收入不符合当前已实现的来源组协议；需要补程序实现，不能通过人工批准收益来源组放行。",
    "BENEFIT_SOURCE_UNPROVED": "退款或收入尚未找到同笔、同币种、金额相反的唯一资产或负债对手。",
    "BENEFIT_CLASSIFICATION_PENDING": "先确定这条退款或收入的预算类别，再审阅其来源。",
    "PLAN_MISSING": "本月有消费的类别缺少目标；请声明该类别目标或覆盖它的父类别子树目标，历史目标无法确认时可明确写 not_assessed。",
    "PURPOSE_TARGET_MISSING": "该用途缺少本月划拨目标；请填写金额，或明确写 not_assessed。",
    "SPENDABLE_CAP_TARGET_MISSING": "最多能花的上限类别缺少本月显式子树消费目标；可填真实额度或明确写 not_assessed，本月不计算上限差。",
    "SPENDABLE_CAP_PENDING": "消费目标差仍待审，最多能花暂时不能签发。",
    "H_HOLDINGS_PENDING": "资产账户用途尚未闭合；核对期初用途和未认领分录。",
    "OPENING_PENDING": "这份期初资产余额尚未完整分配用途。",
    "OWNERSHIP_PENDING": "这条资产分录的资金用途尚未认领。",
    "K_BOOK_PURPOSE_PENDING": "K 账户的期初账面数或申赎用途尚未审定。",
    "K_EVENT_SHAPE_UNSUPPORTED": "该复合申赎交易不符合当前支持的简单现金对手协议，需补程序实现。",
    "K_REDEMPTION_PURPOSE_UNSUPPORTED": "带损益或内扣费用的 K 赎回目前仅支持本金与实收现金归同一用途；混合用途或赎回同时改用途需要补充损益归属协议。",
    "K_REDEMPTION_PURPOSE_MISMATCH": "K 赎回的本金与实收须写同一组 fund 名称，各用途可有损益差额；核对两边 fund，改用途请另记同账户虚拟调拨。",
    "K_CASH_OWNER_PENDING": "基金申赎的现金对手用途尚未审定。",
    "K_EVENT_PENDING": "这笔基金申赎尚未由行内用途或审阅侧文件认领。",
    "PURPOSE_POOL_PENDING": "证券资金池的用途账面分配尚未审定。",
    "OBLIGATION_UNCLAIMED": "这条负债分录尚未认领为消费责任、还款或代垫清算。",
    "OBLIGATION_EVENT_UNSUPPORTED": "该负债交易不符合当前支持的简单消费、还款或代垫协议，需补程序实现。",
    "LIABILITY_OPENING_PENDING": "cutover 前账面余额尚未分配到期初未结债务或代垫 ID；在 obligations 的 opening 中审定来源、剩余金额及债务用途。",
    "OBLIGATION_OPENING_REQUIRED": "这笔还款或报销指向切换前责任，先补 obligations.opening 的对应未结 ID，再重跑。",
    "OBLIGATION_OPENING_SOURCE_UNSUPPORTED": "这份期初往来缺少当前协议可绑定的简单消费、代垫或 Equity 结转来源；借款、复合及跨币期初责任需要继续实现，不应改账伪造来源。",
    "H_BUNDLE_UNBALANCED": "组合交易原币总额不守恒；请核对所有资产腿与费用，不用 Beancount 容差抹平差额。",
    "H_BUNDLE_OWNERSHIP_PENDING": "组合交易的资产用途尚未齐全；请在转账两边写明 fund，不能把转入资金默认当消费。",
    "H_BUNDLE_PURPOSE_AMBIGUOUS": "转账与消费／退款混在同笔且不同用途净额相互抵补，无法区分消费承担与正式改用途；请把用途变更另记同账户虚拟调拨，保留原财务总额。",
    "H_BUNDLE_ALLOCATION_CURRENCY_UNSUPPORTED": "该多分录转账改变了外币用途；报告货币的净划拨计价仍需有来源的兑换协议。",
    "NAMED_H_EVENT_UNSUPPORTED": "用途已声明，但当前资金事件没有可编译的用途协议；需要补程序支持，不能靠重复填写 fund 消除。",
}
IMPLEMENTATION_LIMITS = frozenset((
    "H_BUNDLE_ALLOCATION_CURRENCY_UNSUPPORTED",
    "LIABILITY_CREDIT_MIXED_UNSUPPORTED",
    "LIABILITY_CREDIT_GROUP_UNSUPPORTED",
    "TRANSACTION_ORDER_UNSUPPORTED",
    "DEFAULT_OUTFLOW_EVENT_UNSUPPORTED",
    "SETTLEMENT_PATH_UNSUPPORTED",
    "OBLIGATION_EVENT_UNSUPPORTED", "OBLIGATION_OPENING_SOURCE_UNSUPPORTED",
    "K_EVENT_SHAPE_UNSUPPORTED", "K_REDEMPTION_PURPOSE_UNSUPPORTED",
    "H_TRANSFER_SHAPE_UNSUPPORTED", "H_FX_SHAPE_UNSUPPORTED",
    "NAMED_H_EVENT_UNSUPPORTED",
    "BENEFIT_GROUP_SHAPE_UNSUPPORTED",
))


def review_item(code: str, source: str | None, *, detail: str | None = None,
                **fields) -> dict:
    return {**fields, "reason_code": code,
            "reason_kind": "implementation_limit" if code in IMPLEMENTATION_LIMITS else "data_review",
            "detail": detail or MESSAGES.get(code, "核对这项账本来源或审阅配置。"),
            "source": source}


def posting_sources(fin) -> dict[str, str]:
    return dict(FactIndex(fin).table.sources)


def annotate_source_items(items: list[dict]) -> list[dict]:
    return [review_item(
        item["reason_code"], item.get("source"), detail=item.get("detail"),
        **{key: value for key, value in item.items()
           if key not in ("reason_code", "source", "detail", "reason_kind")})
        for item in items]


def merge_metric_items(metrics: list[dict]) -> list[dict]:
    """One source cause can block several metrics; retain it only once."""
    grouped = {}
    for metric in metrics:
        for item in metric.get("review_items", []):
            key = (item["reason_code"], item["source"], item.get("account"),
                   item.get("target_id"), item.get("commodity"),
                   _key(item.get("ref", {})))
            if key not in grouped:
                grouped[key] = {**item, "metrics": []}
            if metric["metric"] not in grouped[key]["metrics"]:
                grouped[key]["metrics"].append(metric["metric"])
    return list(grouped.values())
