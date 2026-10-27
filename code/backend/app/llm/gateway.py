"""LLM 网关（07 号文档 7.4 节）：所有模型调用都经过这里——计量、预算、模型角色解析。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.llm.base import LLMClient, LLMRequest, LLMResult
from app.config import Settings, get_settings
from app.db import utcnow
from app.models import LlmBatch, LlmUsage


class BudgetExceeded(Exception):
    """非关键任务在超日预算时推迟到次日。"""


class LLMUnavailable(Exception):
    pass


class Gateway:
    def __init__(self, client: LLMClient | None, settings: Settings | None = None):
        self.client = client
        self.settings = settings or get_settings()

    def resolve(self, agent: str, default_role: str) -> tuple[str, str | None]:
        m = self.settings.model_for_agent(agent, default_role)
        return m["model"], m.get("effort")

    def cost_cny(self, model: str, input_tokens: int, output_tokens: int, cache_read: int, batch: bool) -> float:
        p = self.settings.price_for(model)
        usd = (input_tokens * p["input"] + output_tokens * p["output"] + cache_read * p["cache_read"]) / 1_000_000
        if batch:
            usd *= 0.5
        return round(usd * self.settings.budget["usd_to_cny"], 4)

    @staticmethod
    def _day_start() -> datetime:
        # 按北京时间零点切日
        now_cn = utcnow() + timedelta(hours=8)
        return datetime(now_cn.year, now_cn.month, now_cn.day) - timedelta(hours=8)

    @staticmethod
    def _month_start() -> datetime:
        now_cn = utcnow() + timedelta(hours=8)
        return datetime(now_cn.year, now_cn.month, 1) - timedelta(hours=8)

    def _spent_since(self, s: Session, start: datetime, lane: str) -> float:
        return float(s.scalar(select(func.coalesce(func.sum(LlmUsage.cost_cny), 0.0))
                              .where(LlmUsage.at >= start, LlmUsage.lane == lane)))

    def spent_today(self, s: Session, lane: str = "main") -> float:
        # 主流程与单独任务分别计（07 号文档 17.4 节）
        return self._spent_since(s, self._day_start(), lane)

    def spent_month(self, s: Session, lane: str = "main") -> float:
        return self._spent_since(s, self._month_start(), lane)

    def over_budget(self, s: Session) -> str:
        """主流程预算是否已用完；用完时返回说明，否则返回空字符串。"""
        b = self.settings.budget
        if self.spent_today(s) >= b["daily_cny"]:
            return "今日 LLM 花费已达日预算"
        if self.spent_month(s) >= b["monthly_cny"]:
            return "本月 LLM 花费已达月预算"
        return ""

    def check_budget(self, s: Session, agent: str, lane: str = "main") -> None:
        if lane == "adhoc":
            if self.spent_today(s, "adhoc") >= self.settings.adhoc["daily_cny"]:
                raise BudgetExceeded(f"今日单独任务 LLM 花费已达预算，{agent} 推迟到次日")
            return
        if agent in self.settings.budget["critical_agents"]:
            return
        reason = self.over_budget(s)
        if reason:
            raise BudgetExceeded(f"{reason}，{agent} 推迟到次日")

    def record(self, s: Session, agent: str, result: LLMResult, batch: bool = False, lane: str = "main") -> float:
        u = result.usage
        cost = self.cost_cny(result.model, u.input_tokens, u.output_tokens, u.cache_read_tokens, batch)
        s.add(LlmUsage(
            agent=agent, model=result.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens, batch=batch, cost_cny=cost, lane=lane,
        ))
        return cost

    def call(self, s: Session, req: LLMRequest) -> LLMResult:
        if self.client is None:
            raise LLMUnavailable("模型服务未配置（能力 llm 未就绪，见申请清单 R06）")
        self.check_budget(s, req.agent, req.lane)
        result = self.client.run(req)
        if not result.model:
            result.model = req.model
        self.record(s, req.agent, result, lane=req.lane)
        return result

    # ---------- Batch（半价，延迟返回） ----------

    def submit_batch(self, s: Session, agent: str, reqs: dict[str, LLMRequest], resume: dict, max_wait: timedelta) -> LlmBatch:
        """提交 Batch 并登记；resume 为结果到达（或超时）后要继续的作业 {type, payload}。"""
        if self.client is None:
            raise LLMUnavailable("模型服务未配置（能力 llm 未就绪，见申请清单 R06）")
        self.check_budget(s, agent)
        bid = self.client.submit_batch(reqs)
        b = LlmBatch(provider_batch_id=bid, agent=agent, resume=resume, deadline_at=utcnow() + max_wait)
        s.add(b)
        s.flush()
        return b

    def poll_batch(self, s: Session, b: LlmBatch) -> bool:
        """查询一次结果。返回 True 表示已结束（拿到结果或超时改走实时），False 表示还要再等。"""
        if b.status != "submitted":
            return True
        res = self.client.batch_results(b.provider_batch_id) if self.client is not None else None
        if res is None:
            if utcnow() >= b.deadline_at:
                b.status = "fallback"
                return True
            return False
        for r in res.values():
            if r.content:
                self.record(s, b.agent, r, batch=True)
        b.results = {cid: r.model_dump() for cid, r in res.items() if r.content}
        b.status = "ended"
        return True

    @staticmethod
    def batch_result(b: LlmBatch | None, custom_id: str) -> LLMResult | None:
        """Batch 里某个请求的结果；没有（未提交、超时、该请求失败）时返回 None，由调用方走实时调用。"""
        if b is None or custom_id not in (b.results or {}):
            return None
        return LLMResult.model_validate(b.results[custom_id])
