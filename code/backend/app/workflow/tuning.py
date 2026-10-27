"""月度调权 rules.monthly_tuning（07 号文档第 10 节；04 号文档 3.6「规则自优化」）。

按留款 / 下架两组测款比较各维度得分率，提议新的打分权重，发给店主；同意后才保存为规则配置的新版本。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import send_card
from app.models import Candidate, DomainEvent, RuleProposal, User
from app.rules import selection as R
from app.services import Services
from app.workflow import rule_config
from app.workflow.events import subscribe
from app.workflow.jobs import JobContext, job_handler
from app.workflow.state_machine import SYSTEM, transition

log = logging.getLogger(__name__)


def propose_weights(s: Session, period: str) -> RuleProposal | None:
    """样本不足或算出来不用改时返回 None。上个月没处理的提议作废，以这次为准。"""
    rules = get_settings().rules["selection"]
    done = s.scalars(select(Candidate).where(Candidate.status.in_(("kept", "delisted")))).all()
    samples = [(c.breakdown, c.status == "kept") for c in done if c.breakdown]
    old = dict(rules["score_weights"])
    new, stats = R.tune_weights(old, samples, rules["monthly_tuning"])
    if new == old:
        log.info("月度调权 %s：样本 %s 个，不调整", period, len(samples))
        return None
    for p in s.scalars(select(RuleProposal).where(RuleProposal.status == "pending")):
        transition(s, "rule_proposal", p.id, "expire", SYSTEM)
    p = RuleProposal(period=period, patch={"selection": {"score_weights": new}}, summary={
        "samples": len(samples), "kept": sum(ok for _, ok in samples), "delisted": sum(not ok for _, ok in samples),
        "old": old, "new": new, "stats": stats,
    })
    s.add(p)
    s.flush()
    return p


@job_handler("rules.monthly_tuning")
def _monthly_tuning(ctx: JobContext, p: dict[str, Any]) -> None:
    s = ctx.session
    prop = propose_weights(s, p["period"])
    if prop is None:
        return
    card = cards.rule_proposal(prop)
    owners = s.scalars(select(User.feishu_open_id).where(User.role == "owner")).all()
    for oid in owners or [""]:
        send_card(s, ctx.services, card, "rule_proposal", prop.id, to_user=oid)


@subscribe("rule_proposal.applied")
def _on_applied(s: Session, ev: DomainEvent) -> None:
    p = s.get(RuleProposal, ev.entity_id)
    try:
        row = rule_config.save(s, p.patch, by=ev.payload.get("actor", ""))
    except rule_config.RuleError as e:  # 期间店主手动改过规则，导致提议不再合法
        p.summary = {**p.summary, "error": str(e)}
        log.warning("调权提议 #%s 保存失败：%s", p.id, e)
        return
    p.rule_version = row.rule_version
