"""LLM 网关（07 号文档 7.4 节）：所有模型调用都经过这里——计量、预算、模型角色解析。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.llm.base import LLMClient, LLMRequest, LLMResult
from app.config import Settings, get_settings
from app.db import utcnow
from app.models import LlmUsage


class BudgetExceeded(Exception):
    """非关键任务在超日预算时推迟到次日。"""


class LLMUnavailable(Exception):
    pass


class Gateway:
    def __init__(self, client: LLMClient | None, settings: Settings | None = None):
        self.client = client
        self.settings = settings or get_settings()

    def resolve(self, role: str) -> tuple[str, str | None]:
        m = self.settings.model_for(role)
        return m["model"], m.get("effort")

    def cost_cny(self, model: str, input_tokens: int, output_tokens: int, cache_read: int, batch: bool) -> float:
        p = self.settings.price_for(model)
        usd = (input_tokens * p["input"] + output_tokens * p["output"] + cache_read * p["cache_read"]) / 1_000_000
        if batch:
            usd *= 0.5
        return round(usd * self.settings.budget["usd_to_cny"], 4)

    def spent_today(self, s: Session) -> float:
        # 按北京时间零点切日
        now_cn = utcnow() + timedelta(hours=8)
        start = datetime(now_cn.year, now_cn.month, now_cn.day) - timedelta(hours=8)
        return float(s.scalar(select(func.coalesce(func.sum(LlmUsage.cost_cny), 0.0)).where(LlmUsage.at >= start)))

    def check_budget(self, s: Session, agent: str) -> None:
        if agent in self.settings.budget["critical_agents"]:
            return
        if self.spent_today(s) >= self.settings.budget["daily_cny"]:
            raise BudgetExceeded(f"今日 LLM 花费已达预算，{agent} 推迟到次日")

    def record(self, s: Session, agent: str, result: LLMResult, batch: bool = False) -> float:
        u = result.usage
        cost = self.cost_cny(result.model, u.input_tokens, u.output_tokens, u.cache_read_tokens, batch)
        s.add(LlmUsage(
            agent=agent, model=result.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens, batch=batch, cost_cny=cost,
        ))
        return cost

    def call(self, s: Session, req: LLMRequest) -> LLMResult:
        if self.client is None:
            raise LLMUnavailable("模型服务未配置（能力 llm 未就绪，见申请清单 R06）")
        self.check_budget(s, req.agent)
        result = self.client.run(req)
        if not result.model:
            result.model = req.model
        self.record(s, req.agent, result)
        return result
