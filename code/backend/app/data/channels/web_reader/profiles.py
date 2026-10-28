"""专用浏览器（07 号文档 6.3 节）：每个平台一个独立的浏览器用户目录，登录一次长期保存。

- open_login：弹出有界面的浏览器窗口让人登录（扫码、验证码都在这个窗口里完成）
- check_login：无界面打开平台首页，看会不会跳到登录页；结果存在用户目录旁边的 <平台>.login.json

用本机安装的 Edge 或 Chrome（settings.yaml 的 browser），和日常浏览器的登录互不影响。
同一个用户目录同一时刻只能被一个浏览器进程打开，所以登录窗口没关时不做检查。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

_BROWSERS = {
    "edge": ("Edge", [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]),
    "chrome": ("Chrome", [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]),
}
_windows: dict[str, subprocess.Popen] = {}


class BrowserError(Exception):
    pass


@dataclass(frozen=True)
class LoginDef:
    platform: str
    title: str
    capability: str
    url: str
    check_url: str
    logged_out: tuple[str, ...]


def login_defs() -> dict[str, LoginDef]:
    out = {}
    for platform, conf in get_settings().platforms.items():
        lg = conf.get("login") if isinstance(conf, dict) else None
        if lg:
            out[platform] = LoginDef(platform, lg["title"], lg["capability"], lg["url"], lg["check_url"],
                                     tuple(lg["logged_out"]))
    return out


def _get(platform: str) -> LoginDef:
    d = login_defs().get(platform)
    if d is None:
        raise BrowserError(f"没有配置 {platform} 的登录入口")
    return d


def profile_dir(platform: str) -> Path:
    return get_settings().path("browser_profiles_dir") / platform


def _state_file(platform: str) -> Path:
    return get_settings().path("browser_profiles_dir") / f"{platform}.login.json"


def browser_name() -> str:
    return _BROWSERS[get_settings().raw.get("browser", "edge")][0]


def browser_path() -> str:
    name, paths = _BROWSERS[get_settings().raw.get("browser", "edge")]
    for p in [os.environ.get("DSZNT_BROWSER", ""), *paths]:
        if p and Path(p).exists():
            return p
    raise BrowserError(f"没找到 {name}，请先安装（或设置环境变量 DSZNT_BROWSER 指向浏览器的 exe）")


def window_open(platform: str) -> bool:
    p = _windows.get(platform)
    return p is not None and p.poll() is None


def open_login(platform: str) -> None:
    d = _get(platform)
    if window_open(platform):
        raise BrowserError(f"{d.title} 的登录窗口已经开着")
    profile = profile_dir(platform)
    profile.mkdir(parents=True, exist_ok=True)
    _windows[platform] = subprocess.Popen(
        [browser_path(), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--new-window", d.url])


def check_login(platform: str, timeout_s: int = 30) -> dict[str, Any]:
    d = _get(platform)
    if window_open(platform):
        raise BrowserError("请先关掉登录窗口再检查（同一个浏览器目录不能同时打开两次）")
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    profile = profile_dir(platform)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(str(profile), executable_path=browser_path(), headless=True)
            try:
                page = ctx.new_page()
                page.goto(d.check_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                page.wait_for_timeout(3000)  # 等前端跳转
                url = page.url
            finally:
                ctx.close()
    except PlaywrightError as e:
        msg = str(e).splitlines()[0]
        if "user data directory is already in use" in str(e) or "ProcessSingleton" in str(e):
            msg = f"这个浏览器目录正被别的 {browser_name()} 窗口使用，请先关掉"
        raise BrowserError(f"打开 {d.title} 失败：{msg}") from e
    state = {"logged_in": not any(w in url for w in d.logged_out), "url": url.split("?")[0],
             "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _state_file(platform).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def login_state(platform: str) -> dict[str, Any]:
    f = _state_file(platform)
    state = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"logged_in": None, "checked_at": None}
    return {**state, "window_open": window_open(platform)}


def mark_logged_out(platform: str, url: str) -> None:
    """自动读取时遇到登录页、验证或风控：记为未登录，等人工在专用浏览器里处理后再「检查登录」。"""
    state = {"logged_in": False, "url": url.split("?")[0], "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "reason": "自动读取时遇到登录、验证或风控页面"}
    _state_file(platform).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def require_logged_in(platform: str) -> None:
    """健康检查用：只看最近一次的登录状态，不打开网页（省每日页数）。"""
    state = login_state(platform)
    if not state.get("logged_in"):
        raise BrowserError(f"{_get(platform).title} 未登录或需要人工验证，请在「接入状态」里打开浏览器处理后点「检查登录」")
