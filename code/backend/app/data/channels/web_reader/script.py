"""脚本执行器（07 号文档 6.3 节）：脚本是数据（步骤列表），不是代码，每一步执行前都校验。

- 动作白名单：goto（限平台域名）、wait_for、click、set_date、fill_search、select_option、
  read_text、read_table、download_export、screenshot_region、map_columns
- 点击黑名单：目标名称命中「提交 / 保存 / 发布 / 删除……」等写操作词时拒绝
- 每一步之后检查风控：页面文本或 URL 命中验证码 / 访问受限关键词立即停止
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from app.config import get_settings
from app.data.errors import CaptchaRequired, ForbiddenAction, LoginExpired, RiskBlocked, StepFailed

ALLOWED_ACTIONS = {
    "goto", "wait_for", "click", "set_date", "fill_search", "select_option",
    "read_text", "read_table", "download_export", "screenshot_region", "map_columns",
}
_VAR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class Page(Protocol):
    url: str
    text: str

    def goto(self, url: str) -> None: ...
    def find(self, role: str, name: str = "", name_contains: str = "") -> Any: ...
    def click(self, el: Any) -> None: ...
    def fill(self, el: Any, value: str) -> None: ...
    def read_text(self, el: Any) -> str: ...
    def read_table(self, el: Any) -> list[dict[str, Any]]: ...
    def snapshot(self) -> list[dict[str, str]]: ...


@dataclass
class ScriptResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def detect_risk(page: Page) -> None:
    kw = get_settings().platforms["risk_keywords"]
    haystack = f"{page.text or ''} {page.url or ''}"
    if any(w in haystack for w in kw["captcha"]):
        raise CaptchaRequired(f"页面出现验证：{(page.text or '')[:60]}")
    if any(w in haystack for w in kw["blocked"]):
        raise RiskBlocked(f"页面出现风控提示：{(page.text or '')[:60]}")
    if "/login" in urlparse(page.url or "").path:
        raise LoginExpired("跳转到了登录页")


def _render(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            cur: Any = variables
            for part in m.group(1).split("."):
                if not isinstance(cur, dict) or part not in cur:
                    raise StepFailed(f"脚本变量 {m.group(1)} 未配置")
                cur = cur[part]
            return str(cur)
        return _VAR.sub(sub, value)
    if isinstance(value, dict):
        return {k: _render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, variables) for v in value]
    return value


class ScriptRunner:
    """逐步执行脚本，保留中间结果。AI 认路也通过它执行单步，校验规则完全一致。"""

    def __init__(self, page: Page, platform: str, params: dict[str, Any]):
        self.page = page
        self.platform = platform
        pconf = get_settings().platforms[platform]
        self.domains: list[str] = pconf["domains"]
        self.forbidden: list[str] = get_settings().platforms["forbidden_click_words"]
        self.variables = {**params, "urls": pconf.get("urls") or {}}
        self.result = ScriptResult()

    # ----- 校验 -----

    def _check_domain(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        if not any(host == d or host.endswith("." + d) for d in self.domains):
            raise ForbiddenAction(f"不允许访问 {host or url}（{self.platform} 只能访问 {self.domains}）")

    def _check_click(self, target: dict[str, Any], el: Any = None) -> None:
        names = [str(target.get("name", "")), str(target.get("name_contains", ""))]
        if el is not None:
            names += [str(getattr(el, "name", "")), str(getattr(el, "text", ""))]
        for w in self.forbidden:
            if any(w in n for n in names if n):
                raise ForbiddenAction(f"禁止点击含「{w}」的元素")

    def _find(self, target: dict[str, Any], index: int) -> Any:
        el = self.page.find(target.get("role", ""), name=target.get("name", ""), name_contains=target.get("name_contains", ""))
        if el is None:
            raise StepFailed(f"找不到元素 {target}", index)
        return el

    # ----- 执行 -----

    def run_step(self, step: dict[str, Any], index: int = -1) -> None:
        if not isinstance(step, dict) or len(step) != 1:
            raise ForbiddenAction(f"步骤格式不对：{step}")
        action, arg = next(iter(step.items()))
        if action not in ALLOWED_ACTIONS:
            raise ForbiddenAction(f"不在白名单里的动作：{action}")
        arg = _render(arg, self.variables)
        if action == "click":
            self._check_click(arg)
        getattr(self, f"_do_{action}")(arg, index)
        detect_risk(self.page)

    def _do_goto(self, url: str, _i: int) -> None:
        self._check_domain(url)
        self.page.goto(url)

    def _do_wait_for(self, target: dict, i: int) -> None:
        self._find(target, i)

    def _do_click(self, target: dict, i: int) -> None:
        el = self._find(target, i)
        self._check_click(target, el)
        self.page.click(el)

    def _fill(self, arg: dict, i: int) -> None:
        self.page.fill(self._find(arg["target"], i), str(arg.get("value", "")))

    _do_set_date = _do_fill_search = _do_select_option = _fill

    def _do_read_text(self, target: dict, i: int) -> None:
        self.result.texts.append(self.page.read_text(self._find(target, i)))

    def _do_read_table(self, arg: dict, i: int) -> None:
        target = arg.get("target", arg)
        rows = list(self.page.read_table(self._find(target, i)))
        nxt, pages = arg.get("paginate"), 1
        while nxt and pages < int(arg.get("max_pages", 20)):
            btn = self.page.find(nxt.get("role", "button"), name=nxt.get("name", ""), name_contains=nxt.get("name_contains", ""))
            if btn is None or getattr(btn, "disabled", False):
                break
            self._check_click(nxt, btn)
            self.page.click(btn)
            detect_risk(self.page)
            more = self.page.read_table(self._find(target, i))
            if not more:
                break
            rows += more
            pages += 1
        if not rows:
            raise StepFailed(f"表格 {target} 没有数据", i)
        self.result.rows = rows

    def _do_download_export(self, target: dict, i: int) -> None:
        el = self._find(target, i)
        self._check_click(target, el)
        download = getattr(self.page, "download", None)
        if download is None:
            raise StepFailed("当前页面不支持下载导出", i)
        self.result.files.append(str(download(el)))

    def _do_screenshot_region(self, target: dict, i: int) -> None:
        shot = getattr(self.page, "screenshot", None)
        if shot is None:
            raise StepFailed("当前页面不支持截图", i)
        self.result.files.append(str(shot(self._find(target, i))))

    def _do_map_columns(self, mapping: dict[str, str], i: int) -> None:
        if not self.result.rows:
            raise StepFailed("还没有读到表格，无法映射列", i)
        present = set().union(*(r.keys() for r in self.result.rows))
        missing = [c for c in mapping if c not in present]
        if missing:
            raise StepFailed(f"列名对不上：{missing}", i)
        self.result.rows = [{dst: r.get(src) for src, dst in mapping.items()} for r in self.result.rows]


def run_script(page: Page, steps: list[dict[str, Any]], platform: str, params: dict[str, Any]) -> ScriptResult:
    runner = ScriptRunner(page, platform, params)
    for i, step in enumerate(steps):
        runner.run_step(step, i)
    return runner.result
