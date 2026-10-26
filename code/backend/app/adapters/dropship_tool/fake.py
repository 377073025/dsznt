from __future__ import annotations

from datetime import date

from app.adapters.types import AutoOrderStatus, DropshipOrder, ListingPackage, WriteResult


class FakeDropship:
    def __init__(self) -> None:
        self.pushed: list[ListingPackage] = []

    def check_auto_order(self, offer_id: str) -> AutoOrderStatus:
        return AutoOrderStatus(offer_id=offer_id, supported=offer_id != "1688_7000001")

    def push_listing(self, pkg: ListingPackage) -> WriteResult:
        self.pushed.append(pkg)
        return WriteResult(ok=True, ref=f"push_{len(self.pushed)}")

    def order_sync_status(self, day: date) -> list[DropshipOrder]:
        return []
