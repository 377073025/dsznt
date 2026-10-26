"""初始化数据库、店铺与第一个店主。

用法：python scripts/init_db.py --shop "我的店" --owner-open-id ou_xxx [--owner-name 张三]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import bootstrap  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--shop", required=True)
    p.add_argument("--shop-id", default="")
    p.add_argument("--owner-open-id", required=True, help="店主的飞书 open_id（私聊机器人后可在日志里看到）")
    p.add_argument("--owner-name", default="")
    a = p.parse_args()
    bootstrap.init()
    bootstrap.ensure_shop(a.shop, a.shop_id)
    bootstrap.set_owner(a.owner_open_id, a.owner_name)
    print("初始化完成")


if __name__ == "__main__":
    main()
