from __future__ import annotations

from datetime import date
from typing import Protocol

from app.adapters.types import AutoOrderStatus, DropshipOrder, ListingPackage, WriteResult


class DropshipAdapter(Protocol):
    def check_auto_order(self, offer_id: str) -> AutoOrderStatus: ...
    def push_listing(self, pkg: ListingPackage) -> WriteResult: ...  # 写操作，只在 tools/write 可用
    def order_sync_status(self, day: date) -> list[DropshipOrder]: ...
