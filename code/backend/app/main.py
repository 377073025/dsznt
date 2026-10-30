"""web 进程：给 Web 控制台的 REST API。只做查询和发起人工动作，耗时工作交给 worker。

身份：局域网内暂用请求头 X-Open-Id 标识飞书用户（M3 接入飞书登录后替换）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import capabilities as caps
from app.bootstrap import init
from app.config import get_settings
from app.data import queries
from app.data.channels import manual_import
from app.data.channels.web_reader import profiles
from app.db import session_scope
from app.interfaces import console_api
from app.interfaces.feishu.handler import get_or_register
from app.llm.gateway import Gateway
from app.agents import status as agent_status
from app.agents.catalog import AGENT_TITLES, CATALOG
from app.models import AdhocTask, Application, Capability, Job, Report, Shop, Suggestion, Task, User
from app.secrets import get_secret, set_secret
from app.services import get_services
from app.workflow import adhoc
from app.workflow.jobs import enqueue
from app.workflow.state_machine import MODELS, Actor, can


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init(process="web")
    yield


app = FastAPI(title="店铺 AI 闭环 · 控制台 API", lifespan=lifespan)


def db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


def current_user(x_open_id: str = Header(default=""), s: Session = Depends(db)) -> User:
    if not x_open_id:
        raise HTTPException(401, "缺少 X-Open-Id")
    return get_or_register(s, x_open_id)


def require_owner(user: User = Depends(current_user)) -> User:
    if user.role != "owner":
        raise HTTPException(403, "只有店主可以执行该操作")
    return user


@app.get("/api/health")
def health(s: Session = Depends(db)) -> dict[str, Any]:
    capsd = {c.key: c.status for c in s.scalars(select(Capability))}
    return {
        "mode": get_settings().adapter_mode,
        "capabilities": capsd,
        "jobs_failed": len(s.scalars(select(Job.id).where(Job.status == "failed")).all()),
        "llm_cost_today_cny": round(Gateway(None).spent_today(s), 2),
    }


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return {"open_id": user.feishu_open_id, "name": user.name, "role": user.role}


@app.get("/api/products")
def list_products(s: Session = Depends(db)) -> list[dict[str, Any]]:
    """在售商品，供表单下拉选择。"""
    return [{"goods_id": p.goods_id, "name": p.name, "title": p.title, "price_fen": p.price_fen}
            for p in queries.products(s)]


@app.get("/api/capabilities")
def list_capabilities(s: Session = Depends(db)) -> list[dict[str, Any]]:
    unused = set(get_settings().raw.get("unused_capabilities") or [])
    logins = {d.capability: d.platform for d in profiles.login_defs().values()}
    return [
        {"key": c.key, "title": caps.CAPABILITIES[c.key].title, "status": c.status, "applications": c.application_ids,
         "last_error": c.last_error, "credentials": _credential_form(c.key), "unused": c.key in unused,
         "browser": logins.get(c.key)}
        for c in s.scalars(select(Capability))
    ]


@app.get("/api/browsers")
def list_browsers() -> list[dict[str, Any]]:
    return [{"platform": d.platform, "title": d.title, "capability": d.capability, **profiles.login_state(d.platform)}
            for d in profiles.login_defs().values()]


def _browser_action(platform: str, user: User, fn) -> Any:
    if user.role not in ("owner", "operator"):
        raise HTTPException(403, "只有店主和运营可以操作专用浏览器")
    try:
        return fn(platform)
    except profiles.BrowserError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/browsers/{platform}/open")
def open_browser(platform: str, user: User = Depends(current_user)) -> dict:
    _browser_action(platform, user, profiles.open_login)
    return {"platform": platform, **profiles.login_state(platform)}


@app.post("/api/browsers/{platform}/check")
def check_browser(platform: str, s: Session = Depends(db), user: User = Depends(current_user)) -> dict:
    state = _browser_action(platform, user, profiles.check_login)
    services = get_services()
    cap_keys = [profiles.login_defs()[platform].capability]
    # 借用这个浏览器的能力一起更新（例如机会商品用拼多多后台、代发助手王用 1688）
    cap_keys += [k for k, impl in services.browser_caps().items() if impl.platform == platform]
    for cap in dict.fromkeys(cap_keys):
        if cap not in services.health_checks():  # 已有读取实现的平台：登录状态就是通道状态
            continue
        if state["logged_in"]:
            caps.set_status(s, cap, caps.HEALTHY, "专用浏览器已登录")
        elif s.get(Capability, cap).status != caps.NOT_CONFIGURED:
            caps.set_status(s, cap, caps.DEGRADED, "专用浏览器未登录")
    return {"platform": platform, **profiles.login_state(platform)}


def _credential_form(key: str) -> dict[str, Any] | None:
    """录入表单的字段；set 只说明录没录过，不返回值。"""
    form = caps.credential_form(key)
    if form is None:
        return None
    hint, fields = form
    return {"hint": hint, "fields": [{"ref": f.ref, "label": f.label, "secret": f.secret, "placeholder": f.placeholder,
                                       "set": bool(get_secret(f.ref))} for f in fields]}


class CredentialsIn(BaseModel):
    values: dict[str, str]   # 凭证名称 → 值；空值表示不修改


@app.put("/api/capabilities/{key}/credentials")
def put_credentials(key: str, body: CredentialsIn, s: Session = Depends(db), _: User = Depends(require_owner)) -> dict:
    form = caps.credential_form(key)
    if form is None:
        raise HTTPException(400, "这项能力不需要录入凭证")
    refs = [f.ref for f in form[1]]
    values = {r: v.strip() for r, v in body.values.items() if v and v.strip()}
    if unknown := set(values) - set(refs):
        raise HTTPException(400, f"不认识的凭证名称：{'、'.join(sorted(unknown))}")
    if not values:
        raise HTTPException(400, "没有填写任何内容")
    if missing := [f.label for f in form[1] if f.ref not in values and not get_secret(f.ref)]:
        raise HTTPException(400, f"还缺：{'、'.join(missing)}")
    for ref, v in values.items():
        set_secret(ref, v)  # 只进凭据管理器，不落库、不写日志
    caps.mark_configured(s, key, ",".join(refs))
    return {"key": key, "status": caps.CONFIGURED, "saved": sorted(values)}


class CredentialIn(BaseModel):
    ref: str     # 例如 deepseek_api_key、feishu_app_secret
    value: str


@app.post("/api/capabilities/{key}/credential")
def put_credential(key: str, body: CredentialIn, s: Session = Depends(db), _: User = Depends(require_owner)) -> dict:
    if key not in caps.CAPABILITIES:
        raise HTTPException(404, "未知能力")
    set_secret(body.ref, body.value)  # 只进凭据管理器，不落库、不写日志
    caps.mark_configured(s, key, body.ref)
    return {"key": key, "status": caps.CONFIGURED}


@app.get("/api/applications")
def list_applications(s: Session = Depends(db)) -> list[dict[str, Any]]:
    return [{"id": a.id, "name": a.name, "level": a.level, "status": a.status, "note": a.note}
            for a in s.scalars(select(Application).order_by(Application.id))]


class ApplicationPatch(BaseModel):
    status: str | None = None
    note: str | None = None


@app.patch("/api/applications/{app_id}")
def patch_application(app_id: str, body: ApplicationPatch, s: Session = Depends(db), user: User = Depends(current_user)) -> dict:
    if user.role not in ("owner", "operator"):
        raise HTTPException(403, "只有店主和运营可以更新申请进度")
    a = s.get(Application, app_id)
    if a is None:
        raise HTTPException(404, "未知申请编号")
    if body.status:
        a.status = body.status
    if body.note is not None:
        a.note = body.note
    return {"id": a.id, "status": a.status}


@app.get("/api/reports/daily/{day}")
def get_daily_report(day: date, s: Session = Depends(db)) -> dict:
    r = s.scalar(select(Report).where(Report.kind == "daily", Report.period == day.isoformat()))
    if r is None:
        raise HTTPException(404, "该日没有早报")
    return r.content


@app.get("/api/suggestions")
def list_suggestions(status: str | None = None, s: Session = Depends(db)) -> list[dict[str, Any]]:
    q = select(Suggestion).order_by(Suggestion.id.desc())
    if status:
        q = q.where(Suggestion.status == status)
    return [{"id": x.id, "day": x.day.isoformat(), "goods_id": x.goods_id, "action": x.action, "reason": x.reason,
             "status": x.status, "version": x.version} for x in s.scalars(q.limit(100))]


@app.get("/api/tasks")
def list_tasks(s: Session = Depends(db)) -> list[dict[str, Any]]:
    return [{"id": t.id, "executor": t.executor, "status": t.status, "spec": t.spec, "version": t.version}
            for t in s.scalars(select(Task).order_by(Task.id.desc()).limit(100))]


@app.post("/api/{entity}/{obj_id}/actions/{event}")
def act(entity: str, obj_id: int, event: str, s: Session = Depends(db), user: User = Depends(current_user)) -> dict:
    """Web 控制台上的拍板、写入确认：与飞书按钮走同一条路径（card.apply 作业）。"""
    model = MODELS.get(entity)
    obj = s.get(model, obj_id) if model else None
    if obj is None:
        raise HTTPException(404, "对象不存在")
    if not can(entity, obj.status, event, Actor("user", user.feishu_open_id, user.role)):
        raise HTTPException(403, f"无权在状态 {obj.status} 执行 {event}")
    enqueue(s, "card.apply", {"open_id": user.feishu_open_id, "entity": entity, "id": obj_id, "event": event},
            dedupe_key=f"card:{entity}:{obj_id}:{event}:{obj.version}")
    return {"queued": True}


@app.post("/api/imports")
async def upload_import(file: UploadFile = File(...), s: Session = Depends(db), user: User = Depends(current_user)) -> dict:
    if user.role not in ("owner", "operator"):
        raise HTTPException(403, "无权上传数据")
    target_dir = get_settings().path("imports_dir")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / Path(file.filename or "upload.csv").name
    path.write_bytes(await file.read())
    shop = s.scalar(select(Shop).order_by(Shop.id))
    rec = manual_import.import_file(s, shop, path, user.feishu_open_id)
    return {"status": rec.status, "rows": rec.rows, "parser": rec.parser, "errors": rec.error_rows[:20]}


# ---------- 智能体工作台（07 号文档 17.6 节）。Web 每 5 秒轮询 ----------

@app.get("/api/agents")
def list_agents(s: Session = Depends(db)) -> list[dict[str, Any]]:
    return agent_status.agents_status(s)


@app.get("/api/agents/{agent}/runs")
def agent_runs(agent: str, source: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    if agent not in AGENT_TITLES:
        raise HTTPException(404, "未知智能体")
    return agent_status.runs_of(agent, source, min(limit, 200), offset)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    d = agent_status.run_detail(run_id)
    if d is None:
        raise HTTPException(404, "运行记录不存在")
    return d


@app.get("/api/agent-capabilities")
def list_agent_capabilities() -> list[dict[str, Any]]:
    """可单独调用的智能体能力与参数表单（/api/capabilities 已用于能力开关，所以换个路径）。"""
    return [{"key": c.key, "agent": c.agent, "title": c.title, "description": c.description,
             "params_schema": c.params.model_json_schema(), "uses_web": sorted(c.uses_web), "promote_to": c.promote_to}
            for c in CATALOG.values() if c.adhoc]


class AdhocIn(BaseModel):
    capability: str
    params: dict[str, Any] = {}
    note: str = ""


def _adhoc_dict(t: AdhocTask) -> dict[str, Any]:
    return {"id": t.id, "agent": t.agent, "capability": t.capability, "title": CATALOG[t.capability].title,
            "params": t.params, "note": t.note, "submitted_by": t.submitted_by, "status": t.status, "error": t.error,
            "result": t.result, "run_id": t.run_id, "promoted_to": t.promoted_to, "can_promote": adhoc.can_promote(t),
            "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat()}


@app.post("/api/adhoc")
def submit_adhoc(body: AdhocIn, s: Session = Depends(db), user: User = Depends(current_user)) -> dict[str, Any]:
    from pydantic import ValidationError

    try:
        return _adhoc_dict(adhoc.submit(s, user, body.capability, body.params, body.note))
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False)) from e
    except adhoc.AdhocError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/adhoc")
def list_adhoc(status: str | None = None, s: Session = Depends(db)) -> list[dict[str, Any]]:
    q = select(AdhocTask).order_by(AdhocTask.id.desc())
    if status:
        q = q.where(AdhocTask.status == status)
    return [_adhoc_dict(t) for t in s.scalars(q.limit(100))]


@app.post("/api/adhoc/{task_id}/cancel")
def cancel_adhoc(task_id: int, s: Session = Depends(db), user: User = Depends(current_user)) -> dict[str, Any]:
    try:
        return _adhoc_dict(adhoc.cancel(s, user, task_id))
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except adhoc.AdhocError as e:
        raise HTTPException(400, str(e)) from e


class PromoteIn(BaseModel):
    indexes: list[int] | None = None  # 诊断结果里要转的建议序号；不填为全部


@app.post("/api/adhoc/{task_id}/promote")
def promote_adhoc(task_id: int, body: PromoteIn | None = None, s: Session = Depends(db),
                  user: User = Depends(current_user)) -> dict[str, Any]:
    try:
        return {"promoted_to": adhoc.promote(s, get_services(), user, task_id, body.indexes if body else None)}
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except adhoc.AdhocError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/jobs/{job_type}")
def trigger(job_type: str, payload: dict[str, Any], s: Session = Depends(db), _: User = Depends(require_owner)) -> dict:
    """手动触发作业，例如 daily.fetch {"day": "2026-10-23"}。"""
    job = enqueue(s, job_type, payload)
    return {"job_id": job.id if job else None}


app.include_router(console_api.api)

class _ConsoleFiles(StaticFiles):
    """每次都向服务器确认版本（文件没变时返回 304），升级后不会继续用浏览器里的旧脚本。"""

    def file_response(self, *args: Any, **kwargs: Any):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Web 控制台静态页（无构建步骤）。放在最后挂载，不遮挡 /api 路由。
app.mount("/", _ConsoleFiles(directory=Path(__file__).parent / "static", html=True), name="console")
