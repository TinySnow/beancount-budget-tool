"""Readable review reasons and source positions shared by monthly diagnostics."""

from __future__ import annotations

from beancount.core import data

from .classification import _key
from .facts import FactIndex


MESSAGES = {
    "ACCOUNT_UNREVIEWED": "该资产账户尚未声明为 H 真实库存或 K 申赎累计账户。",
    "PAYMENT_SCOPE_PENDING": "该账户的此币种能否直接支付尚未确定；审定 payable 或 excluded。",
    "CLASSIFICATION_UNREVIEWED": "这条收入或费用缺少行内 budget、侧文件认领或已审默认分类。",
    "OWNERSHIP_UNREVIEWED": "这条资产分录缺少 fund、侧文件用途认领或可确定的默认用途。",
    "POOL_BOUNDARY_PENDING": "这笔资金跨越证券池边界，需要确认现金转入或转出的用途。",
    "H_TRANSFER_PENDING": "这笔资产转账尚未完成用途审阅。",
    "H_TRANSFER_OWNER_PENDING": "资产转账的一侧或两侧资金用途尚未认领。",
    "H_TRANSFER_MIXED_REMAP_PENDING": "跨账户转账的多用途金额不一致；核对两侧 fund，重新分配用途须另记同账户虚拟调拨。",
    "H_TRANSFER_SHAPE_UNSUPPORTED": "该复合资产转账不符合当前简单转账协议，需补程序实现。",
    "H_FX_SHAPE_UNSUPPORTED": "该复合购汇交易不符合当前简单购汇协议，需补程序实现。",
    "PAYABLE_CASH_PENDING": "可直接支付账户的用途库存尚未审定；核对期初用途、分录归属或支付范围。",
    "BOOKED_OBLIGATIONS_PENDING": "已入账责任尚未审定；核对消费责任、还款和代垫清算的来源。",
    "BOOKED_LIABILITY_UNMATCHED": "该账户的账本负债余额与已审责任未偿额不一致。",
    "CLEARING_NONZERO": "这笔单位代垫尚未结清，当前实际可花计算尚不支持未结代垫。",
    "SETTLEMENT_VALUE_MISSING": "外币责任尚未清偿；当前实际可花入口尚未支持报告日外币清偿价值。",
    "DEBT_COVERAGE_PENDING": "专项责任缺少覆盖决定；在 debt_coverage 中逐笔声明专用支付资源，或明确 resources: [] 表示没有专用覆盖。",
    "EXTERNAL_SCOPE_PENDING": "缺少本月末的完整性声明；核对截至该日已发生但漏记的责任或在途付款，再提供 completeness。",
    "CLASSIFICATION_PENDING": "这条收入或费用尚未确定预算类别。",
    "VALUATION_MISSING": "这条外币消费缺少有来源的报告货币计价或已审核的实际还款成本。",
    "BENEFIT_SOURCE_PENDING": "退款或收入的经济利益来源尚未审定。",
    "BENEFIT_GROUP_REQUIRED": "这笔多分录退款或收入需要审阅来源分组，防止本金与收益重复计入。",
    "BENEFIT_SOURCE_UNPROVED": "退款或收入尚未找到同笔、同币种、金额相反的唯一资产或负债对手。",
    "BENEFIT_CLASSIFICATION_PENDING": "先确定这条退款或收入的预算类别，再审阅其来源。",
    "PLAN_MISSING": "本月有消费的类别缺少目标；请声明该类别目标或覆盖它的父类别子树目标。",
    "PURPOSE_TARGET_MISSING": "该用途缺少本月划拨目标；请填写金额，或明确写 not_assessed。",
    "SPENDABLE_CAP_TARGET_MISSING": "最多能花的上限类别缺少本月显式子树消费目标。",
    "SPENDABLE_CAP_PENDING": "消费目标差仍待审，最多能花暂时不能签发。",
    "H_HOLDINGS_PENDING": "资产账户用途尚未闭合；核对期初用途和未认领分录。",
    "OPENING_PENDING": "这份期初资产余额尚未完整分配用途。",
    "OWNERSHIP_PENDING": "这条资产分录的资金用途尚未认领。",
    "K_BOOK_PURPOSE_PENDING": "K 账户的期初账面数或申赎用途尚未审定。",
    "K_EVENT_SHAPE_UNSUPPORTED": "该复合申赎交易不符合当前支持的简单现金对手协议，需补程序实现。",
    "K_CASH_OWNER_PENDING": "基金申赎的现金对手用途尚未审定。",
    "K_EVENT_PENDING": "这笔基金申赎尚未由行内用途或审阅侧文件认领。",
    "PURPOSE_POOL_PENDING": "证券资金池的用途账面分配尚未审定。",
    "OBLIGATION_UNCLAIMED": "这条负债分录尚未认领为消费责任、还款或代垫清算。",
    "OBLIGATION_EVENT_UNSUPPORTED": "该负债交易不符合当前支持的简单消费、还款或代垫协议，需补程序实现。",
    "LIABILITY_OPENING_PENDING": "cutover 前的未清负债需要期初责任协议，当前入口尚未实现该协议。",
    "NAMED_H_EVENT_UNSUPPORTED": "该资金事件尚未匹配已审用途协议；核对责任、转账、申赎或购汇审阅。",
}
IMPLEMENTATION_LIMITS = frozenset((
    "CLEARING_NONZERO", "SETTLEMENT_VALUE_MISSING",
    "OBLIGATION_EVENT_UNSUPPORTED", "LIABILITY_OPENING_PENDING",
    "K_EVENT_SHAPE_UNSUPPORTED",
    "H_TRANSFER_SHAPE_UNSUPPORTED", "H_FX_SHAPE_UNSUPPORTED",
))


def review_item(code: str, source: str | None, *, detail: str | None = None,
                **fields) -> dict:
    return {**fields, "reason_code": code,
            "reason_kind": "implementation_limit" if code in IMPLEMENTATION_LIMITS else "data_review",
            "detail": detail or MESSAGES.get(code, "核对这项账本来源或审阅配置。"),
            "source": source}


def posting_sources(fin) -> dict[str, str]:
    facts = FactIndex(fin)
    result = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            meta = posting.meta or entry.meta
            result[_key(facts.for_posting(posting).as_dict())] = (
                f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}")
    return result


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
