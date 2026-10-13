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

from ..errors import require
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
