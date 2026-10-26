"""Explicit, source-bound terminal decisions for purchases and simple payments."""

from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from beancount.core import data
import yaml

from ..errors import BudgetError, require
from ..exact import amount_text
from ..yamlstrict import load_yaml
from .classification import _key
from .facts import FactIndex
from .input_compiler import (check_unchanged, month_inputs, publish_inputs, read_month_config,
                             relative_reference, report_saved_inputs)
from .monthly_report import audit_month_workflow
from .obligations import audit_simple_obligations
from .ownership import _owners


@dataclass
class PreparedObligationReview:
    config: Path
    row: dict
    inputs: dict
    choices: dict
    aliases: dict
    initial: str

    @property
    def purchase(self):
        return self.row["shape"] == "consumer_purchase"

    @property
    def receipt(self):
        return self.row["shape"] == "asset_inflow"

    @property
    def label(self):
        return ("新增消费责任的用途" if self.purchase else "新增应付款的责任用途" if self.receipt else
                "本次归还的责任（编号）")

    @property
    def choice_labels(self):
        if self.purchase or self.receipt:
            return sorted(self.aliases)
        return [f"{index}. {row['label']}" for index, row in enumerate(self.choices.values(), 1)]

    @property
    def guidance(self):
        if self.receipt:
            return "确认本次到账全额形成须留作归还的应付款，不是收入、收回代垫或一般贷款／分期；资产 fund 仍另审，不凭此新增可花或 G。"
        if any("same_day_source_ref" in choice for choice in self.choices.values()):
            return "标为同日清算的选项只确认付款与责任的对应关系，不确认先后；原顺序若先付，就记当日预付并在同日责任出现后冲销。不会借用未来现金，不可跨日。"
        return ("只确认这条卡债由哪个已登记用途承担；不填 fund，不拆分金额，不改变现金或 G。" if self.purchase else
                "选择本条负债分录归还的既有责任；合并付款须逐条选择，现金只计一次。不按金额或 FIFO 猜配，不作单位代垫。")


def _resolve(prepared, decision):
    require(type(decision) is str and bool(decision.strip()), "REVIEW_DECISION_INVALID", "请选择一个可用项目", prepared.row["source"])
    decision = decision.strip()
    if prepared.purchase or prepared.receipt:
        ident = prepared.aliases.get(decision)
    else:
        identifiers = list(prepared.choices)
        ident = (identifiers[int(decision) - 1] if decision.isascii() and decision.isdecimal() and
                 1 <= int(decision) <= len(identifiers) else decision)
    require(ident in prepared.choices, "REVIEW_DECISION_INVALID",
            "项目未登记或不在本笔之前的可用责任中；请使用界面列出的选项", prepared.row["source"])
    return ident


def validate_decision(prepared, decision, reason):
    require(type(reason) is str and bool(reason.strip()), "REVIEW_REASON_REQUIRED", "请填写判断依据", prepared.row["source"])
    ident = _resolve(prepared, decision)
    return [(prepared.choices[ident]["label"], abs(Fraction(prepared.row["signed_units"])))]


def prepare_review(config: Path, item: dict) -> PreparedObligationReview:
    require(item.get("kind") == "booked_obligation" and item.get("reason_code") == "OBLIGATION_UNCLAIMED" and
            item.get("shape") in ("consumer_purchase", "asset_outflow", "asset_inflow"), "TUI_REVIEW_UNSUPPORTED",
            "此入口只审消费责任、新增应付款及已支持的还款；代垫、期初及实现缺口须分别处理", item.get("source"))
    config = config.resolve()
    doc, paths, cutover = read_month_config(config)
    inputs = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    with month_inputs(paths["ledger"], doc["month"], cutover) as (fin, last):
        current = audit_month_workflow(config)
        selected = next((row for row in current["review_items"] if row.get("kind") == "booked_obligation" and
                         _key(row.get("ref")) == _key(item["ref"])), None)
        require(selected is not None and selected["reason_code"] == "OBLIGATION_UNCLAIMED" and
                selected["shape"] == item["shape"],
                "REVIEW_SOURCE_CHANGED", "选中来源已变或已被认领；请返回清单重新选择", item.get("source"))
        audit = audit_simple_obligations(paths["ledger"], cutover, paths["obligations"], last)
        event = next(row for row in audit["events"] if _key(row["liability_ref"]) == _key(selected["ref"]))
        facts = FactIndex(fin)
        entry = next(entry for entry in fin.entries if isinstance(entry, data.Transaction) and
                     entry.date.isoformat() == selected["date"] and any(
            _key(facts.for_posting(p).as_dict()) == _key(selected["ref"]) for p in entry.postings))
        row = {**selected, "payee": entry.payee, "narration": entry.narration, "event": event}
        owners = load_yaml(paths["obligations"])["owners"]
        aliases = _owners(owners)
        names = {owner["id"]: owner["name"] for owner in owners}
        choices = {owner: {"label": name} for owner, name in names.items()}
        default = load_yaml(paths["ownership"]).get("defaults", {}).get("expense_owner_id")
        initial = names.get(default, "")
        if event["shape"] == "asset_outflow":
            prefix = audit_simple_obligations(paths["ledger"], cutover, paths["obligations"], last,
                                              before_ref=_key(event["liability_ref"]))
            coordinate = tuple(event[key] for key in ("account", "commodity", "lot"))
            same = lambda record: tuple(record[key] for key in ("account", "commodity", "lot")) == coordinate
            require(not any(same(record) for record in prefix["opening_pending"]),
                    "TUI_OBLIGATION_REVIEW_PENDING", "同账户／币种的期初责任尚未审定；请先处理期初项目", row["source"])
            prior = next((record for record in prefix["events"] if same(record) and record["status"] != "CERTIFIED"), None)
            if prior is not None:
                from .review_diagnostics import posting_sources
                earlier = posting_sources(fin)[_key(prior["liability_ref"])]
                require(False, "TUI_OBLIGATION_REVIEW_PENDING",
                        f"同账户／币种有更早的未审责任事件：{prior['date']}，{earlier}。先审该项，再审本条；清单展示顺序不代表重放顺序",
                        row["source"])
            amount = Fraction(event["signed_units"])
            # Choices use the whole pre-transaction snapshot, minus decisions
            # already saved for sibling legs of this same observed payment.
            allocated = {}
            if event.get("proof") == "native_group_payment_v1":
                for sibling in audit["events"]:
                    if (sibling.get("payment_group_ref") == event["payment_group_ref"] and
                            sibling["status"] == "CERTIFIED"):
                        ident = sibling["obligation_id"]
                        allocated[ident] = allocated.get(ident, Fraction(0)) + Fraction(sibling["signed_units"])
            available = [{**record, "remaining_native": str(Fraction(record["remaining_native"]) -
                          allocated.get(record["obligation_id"], Fraction(0)))} for record in prefix["obligations"]]
            eligible = sorted((record for record in available
                               if same(record) and Fraction(record["remaining_native"]) >= amount),
                              key=lambda record: (record.get("purchase_date") or "", record["obligation_id"]))
            earlier_sources = {_key(record["liability_ref"]) for record in prefix["events"]}
            later_sources = {_key(record["liability_ref"]) for record in audit["events"]
                             if record["date"] == event["date"] and record["status"] == "CERTIFIED" and
                             record["kind"] == "consumer_purchase" and same(record) and
                             _key(record["liability_ref"]) not in earlier_sources}
            same_day = sorted((record for record in audit["obligations"] if same(record) and
                               _key(record["purchase_ref"]) in later_sources and
                               Fraction(record["remaining_native"]) >= amount), key=lambda record: record["obligation_id"])
            wanted = {_key(record["purchase_ref"]) for record in [*eligible, *same_day]}
            descriptions = {}
            for entry in fin.entries:
                if not isinstance(entry, data.Transaction):
                    continue
                for posting in entry.postings:
                    if posting.account == event["account"] and _key(facts.for_posting(posting).as_dict()) in wanted:
                        meta = posting.meta or entry.meta
                        descriptions[_key(facts.for_posting(posting).as_dict())] = (
                            f"{entry.payee or ''} {entry.narration} · {Path(meta['filename']).name}:{meta['lineno']}").strip()
            choices = {record["obligation_id"]: {"label":
                f"{record.get('purchase_date') or '期初'} · {names[record['owner_id']]} · "
                f"本笔前未偿 {amount_text(record['remaining_native'])} {record['commodity']} · "
                f"{descriptions.get(_key(record['purchase_ref']), '已审期初责任')}"}
                for record in eligible}
            choices.update({record["obligation_id"]: {
                "same_day_source_ref": record["purchase_ref"],
                "label": f"同日清算（先后未知） · {record['purchase_date']} · {names[record['owner_id']]} · "
                f"尚可清偿 {amount_text(record['remaining_native'])} {record['commodity']} · "
                f"{descriptions[_key(record['purchase_ref'])]}"} for record in same_day})
            require(bool(choices), "TUI_OBLIGATION_REVIEW_PENDING",
                    "没有可单独覆盖本条分录的已审责任；先审来源／期初。同日先后不明可先确认同日消费责任，再明确选择同日清算；跨日预付和单腿分摊仍需专门协议，不要拆改真实交易", row["source"])
            initial = "1" if len(choices) == 1 and not same_day else ""
    check_unchanged(inputs)
    return PreparedObligationReview(config, row, inputs, choices, aliases, initial)


def save_review(prepared, decision, reason, output: Path, *, approved=False):
    require(approved is True, "REVIEW_APPROVAL_REQUIRED", "须明确批准本项后才能保存")
    validate_decision(prepared, decision, reason)
    ident = _resolve(prepared, decision)
    check_unchanged(prepared.inputs)
    fresh = prepare_review(prepared.config, prepared.row)
    require(ident in fresh.choices, "REVIEW_SOURCE_CHANGED", "所选用途或未清责任已改变；请重新审阅", prepared.row["source"])
    require(prepared.choices[ident].get("same_day_source_ref") == fresh.choices[ident].get("same_day_source_ref"),
            "REVIEW_SOURCE_CHANGED", "同日清算来源或顺序模式已改变；请重新审阅", prepared.row["source"])
    doc, paths, cutover = read_month_config(prepared.config)
    before = {path: path.read_bytes() for path in {prepared.config, *paths.values()} if path != paths["ledger"]}
    output = output.resolve()
    require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已存在；请选择新目录", str(output))
    document = load_yaml(paths["obligations"])
    event = fresh.row["event"]
    counterpart = "counterpart_refs" if "counterpart_refs" in event else "counterpart_ref"
    claim = {"liability_ref": event["liability_ref"], counterpart: event[counterpart], "reason": reason.strip(),
             "kind": "consumer_purchase" if fresh.purchase else "payable_receipt" if fresh.receipt else "payment",
             "obligation_id": "debt_" + sha256(_key(event["liability_ref"]).encode()).hexdigest() if fresh.purchase or fresh.receipt else ident,
             **({"owner_id": ident} if fresh.purchase or fresh.receipt else {})}
    if "same_day_source_ref" in fresh.choices[ident]:
        claim["same_day_source_ref"] = fresh.choices[ident]["same_day_source_ref"]
    document["events"].append(claim)
    with month_inputs(paths["ledger"], doc["month"], cutover) as (_, last):
        with TemporaryDirectory(prefix="budget-debt-review-") as folder:
            candidate = (Path(folder) / "obligations.yaml").resolve()
            candidate.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
            try:
                validated = audit_simple_obligations(paths["ledger"], cutover, candidate, last)
            except BudgetError as exc:
                if (exc.source or "").startswith(str(candidate)):
                    raise BudgetError(exc.code, exc.message, fresh.row["source"]) from exc
                raise
            saved = next(row for row in validated["events"] if row["liability_ref"] == event["liability_ref"])
            require(saved["status"] == "CERTIFIED", "REVIEW_DECISION_INVALID", "本项未通过正式责任审计，未保存", fresh.row["source"])
        check_unchanged(before)
        check_unchanged(prepared.inputs)
    target = output / "reviewed"
    published = {**doc, **{key: relative_reference(path, target) for key, path in paths.items()}, "obligations": "obligations.yaml"}
    publish_inputs(target, {"month.yaml": published, "obligations.yaml": document})
    result = {"status": "DECISIONS_SAVED", "config": str(target / "month.yaml"), "accepted": [{
        "kind": "booked_obligation", "source": fresh.row["source"], "decision": decision,
        "obligation_id": claim["obligation_id"]}]}
    return report_saved_inputs(result, audit_month_workflow, before, prepared.inputs, io_error="REVIEW_IO_ERROR")
