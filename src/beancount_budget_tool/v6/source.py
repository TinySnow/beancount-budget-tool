"""v6 第一阶段：复用官方 loader，但单独校验 v6 的原文输入边界。

本层只冻结与检查源事实，不分类、不猜用途、不输出预算数。
旧账本中尚未迁移的无标签 posting 被计入待审数量；非法的旧
交易级 ``budget``/``fund`` 则必须明确拒绝，不能由 loader 接受
就当作 v6 合法输入。
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

from beancount.core import data

from ..errors import BudgetError, require
from ..financial import load_financial
from ..exact import exact
from .inline_amounts import parse_inline_amounts


@dataclass(frozen=True)
class SourceAudit:
    digest: str
    loader_version: str
    cutover: date
    source_files: int
    transactions: int
    postings: int
    missing_inline_classification: int
    missing_inline_asset_intent: int


def _root(account: str, options: Mapping[str, Any]) -> str:
    prefix = account.split(":", 1)[0]
    for kind in ("assets", "expenses", "income"):
        if prefix == options[f"name_{kind}"]:
            return kind
    return "other"


def _source(meta: Mapping[str, Any]) -> str:
    return f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"


def _tag_text(value: object, key: str, source: str) -> str:
    require(isinstance(value, str) and bool(value) and value == value.strip(),
            "TAG_SYNTAX_INVALID", f"{key} 必须是非空、无首尾空白的字符串", source)
    return value


def _check_tracking(value: object, source: str) -> None:
    raw = _tag_text(value, "tracking", source)
    require("，" not in raw and all(part and part == part.strip() for part in raw.split(",")),
            "TRACKING_INVALID", "tracking 只接受非空、半角逗号分隔的专题", source)


def collect_v6_source_issues(root: Path, cutover: date,
                             through: Optional[date] = None) -> list[dict]:
    """Collect every invalid inline v6 tag in a period before deeper audits.

    This preflight reports lexical problems only. It must not infer a budget
    category or an asset purpose from legacy transaction metadata.
    """
    fin = load_financial(root)
    issues = []

    def check(entry: data.Transaction, account: str, source: str,
              operation) -> None:
        try:
            operation()
        except BudgetError as exc:
            issues.append({"kind": "source_tag", "date": entry.date.isoformat(),
                           "account": account, "source": source,
                           "reason_code": exc.code, "detail": exc.message})

    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover or (
                through is not None and entry.date > through):
            continue
        meta = entry.meta or {}
        tx_source = _source(meta)
        for key in ("budget", "fund"):
            if key in meta:
                issues.append({"kind": "source_tag", "date": entry.date.isoformat(),
                               "account": "(transaction)", "source": tx_source,
                               "reason_code": "TAG_LEVEL_FORBIDDEN",
                               "detail": f"交易级 {key} 须移到对应分录，旧含义待人工复核"})
        if "tracking" in meta:
            check(entry, "(transaction)", tx_source,
                  lambda: _check_tracking(meta["tracking"], tx_source))
        for posting in entry.postings:
            pmeta = posting.meta or {}
            psource = _source(pmeta)
            kind = _root(posting.account, fin.options)
            for key, valid in (("budget", kind in ("expenses", "income")),
                               ("fund", kind == "assets")):
                if key not in pmeta:
                    continue
                if not valid:
                    issues.append({"kind": "source_tag", "date": entry.date.isoformat(),
                                   "account": posting.account, "source": psource,
                                   "reason_code": "TAG_LEVEL_FORBIDDEN",
                                   "detail": f"{key} 不允许写在此类分录"})
                    continue

                def validate_tag(key=key, value=pmeta[key], source=psource,
                                 posting=posting) -> None:
                    raw = _tag_text(value, key, source)
                    parse_inline_amounts(raw, exact(posting.units.number), source,
                                         code="FUND_GRAMMAR" if key == "fund" else
                                              "TAG_SYNTAX_INVALID")

                check(entry, posting.account, psource, validate_tag)
            if "tracking" in pmeta:
                check(entry, posting.account, psource,
                      lambda: _check_tracking(pmeta["tracking"], psource))
    return issues


def scan_v6_source(root: Path, cutover: date, through: Optional[date] = None) -> SourceAudit:
    """验证 loader、include 快照和已出现的 v6 标签，返回迁移盘点。

    没有 inline 标签不一定是错误：分类／归属可能来自将来经审阅
    的侧文件，K 腿的 ``fund`` 还须由申赎事件与现金腿共同校验。
    因此这两项只统计数量；
    真正的 v6 编译器须在侧文件与账户语义合并后逐腿证明穷尽。
    """
    require(isinstance(cutover, date) and cutover.day == 1,
            "CUTOVER_INVALID", "v6 cutover 必须是自然月第一天")
    require(through is None or (isinstance(through, date) and through >= cutover),
            "TIME_INVALID", "来源审计结束日不得早于 cutover")
    fin = load_financial(root)
    tx_count = post_count = missing_class = missing_asset = 0
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover or (
                through is not None and entry.date > through):
            continue
        tx_count += 1
        meta = entry.meta or {}
        source = _source(meta)
        require("budget" not in meta and "fund" not in meta,
                "TAG_LEVEL_FORBIDDEN", "v6 budget/fund 只能写在对应 posting，不得写在交易级", source)
        if "tracking" in meta:
            _check_tracking(meta["tracking"], source)
        for posting in entry.postings:
            post_count += 1
            pmeta = posting.meta or {}
            psource = _source(pmeta)
            kind = _root(posting.account, fin.options)
            require("budget" not in pmeta or kind in ("expenses", "income"),
                    "TAG_LEVEL_FORBIDDEN", "budget 只能写在费用／收入 posting", psource)
            require("fund" not in pmeta or kind == "assets",
                    "TAG_LEVEL_FORBIDDEN", "fund 只能写在资产 posting", psource)
            if "budget" in pmeta:
                raw = _tag_text(pmeta["budget"], "budget", psource)
                parse_inline_amounts(raw, exact(posting.units.number), psource)
            elif kind in ("expenses", "income"):
                missing_class += 1
            if "fund" in pmeta:
                raw = _tag_text(pmeta["fund"], "fund", psource)
                parse_inline_amounts(raw, exact(posting.units.number), psource,
                                     code="FUND_GRAMMAR")
            elif kind == "assets":
                missing_asset += 1
            if "tracking" in pmeta:
                _check_tracking(pmeta["tracking"], psource)
    return SourceAudit(fin.digest, fin.loader_version, cutover, len(fin.sources),
                       tx_count, post_count, missing_class, missing_asset)
