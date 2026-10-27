"""每日备份（07 号文档 2.2 节）：数据库快照 + 素材库增量，保留 30 天。

目录结构（backup.dir，建议放到另一块盘或网盘同步目录）：
  <dir>/db/YYYYMMDD/app.db、app.runs.db   每天一份数据库快照（SQLite 在线备份，不停服务）
  <dir>/assets/                           素材库镜像：只复制新增或改过的文件，源目录删掉的文件这里保留
超过 keep_days 的数据库快照删除；素材镜像不删。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import BACKEND_DIR, get_settings
from app.db import get_engine, runs_url_for, utcnow

log = logging.getLogger(__name__)


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///") or ":memory:" in url:
        return None
    return Path(url.removeprefix("sqlite:///"))


def _copy_db(src: Path, dst: Path) -> None:
    """用 SQLite 在线备份接口复制，写入中的数据库也能得到一致的快照。"""
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def _mirror(src: Path, dst: Path) -> int:
    n = 0
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        target = dst / f.relative_to(src)
        st = f.stat()
        if target.exists() and target.stat().st_size == st.st_size and target.stat().st_mtime >= st.st_mtime:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        n += 1
    return n


def backup_dir() -> Path:
    p = Path(get_settings().raw["backup"]["dir"])
    return p if p.is_absolute() else BACKEND_DIR / p


def run_backup(now: datetime | None = None, root: Path | None = None, assets: Path | None = None) -> dict[str, Any]:
    """root / assets 默认取配置；数据库取当前进程实际连接的库。"""
    settings = get_settings()
    conf = settings.raw["backup"]
    now_cn = (now or utcnow()) + timedelta(hours=8)
    root = root or backup_dir()
    url = get_engine().url
    db_url = f"sqlite:///{url.database}" if url.get_backend_name() == "sqlite" and url.database else str(url)
    out: dict[str, Any] = {"dir": str(root), "databases": [], "assets_copied": 0, "pruned": []}

    db_path = _sqlite_path(db_url)
    if db_path is None:
        log.warning("数据库不是 SQLite 文件，跳过数据库备份（请用数据库自带的备份工具）")
    else:
        day_dir = root / "db" / now_cn.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        for src in (db_path, _sqlite_path(runs_url_for(db_url))):
            if src is not None and src.exists():
                _copy_db(src, day_dir / src.name)
                out["databases"].append(src.name)

    assets = assets or settings.path("assets_dir")
    if assets.exists():
        out["assets_copied"] = _mirror(assets, root / "assets")

    cutoff = (now_cn - timedelta(days=conf["keep_days"])).strftime("%Y%m%d")
    for d in sorted((root / "db").glob("*")) if (root / "db").exists() else []:
        if d.is_dir() and d.name.isdigit() and d.name < cutoff:
            shutil.rmtree(d)
            out["pruned"].append(d.name)
    return out
