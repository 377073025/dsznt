from __future__ import annotations

from pathlib import Path

from app.adapters import fixtures
from app.adapters.types import OfferBrief, OfferDetail, OfferFilter, SupplierProfile


class FakeSourcing:
    def search_by_keyword(self, kw: str, filters: OfferFilter, limit: int) -> list[OfferBrief]:
        return fixtures.offer_search(kw)[:limit]

    def search_by_image(self, image: Path, limit: int) -> list[OfferBrief]:
        return fixtures.offer_search("")[:limit]

    def offer_detail(self, offer_id: str) -> OfferDetail:
        return fixtures.offer_detail(offer_id)

    def supplier_profile(self, member_id: str) -> SupplierProfile:
        return SupplierProfile(member_id=member_id, years=5, ship_48h_rate=0.98, repurchase_rate=0.32)
