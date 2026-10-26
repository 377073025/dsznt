from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.adapters.types import OfferBrief, OfferDetail, OfferFilter, SupplierProfile


class SourcingAdapter(Protocol):
    """1688 货源只读接口。"""

    def search_by_keyword(self, kw: str, filters: OfferFilter, limit: int) -> list[OfferBrief]: ...
    def search_by_image(self, image: Path, limit: int) -> list[OfferBrief]: ...
    def offer_detail(self, offer_id: str) -> OfferDetail: ...
    def supplier_profile(self, member_id: str) -> SupplierProfile: ...
