"""手工录入的 1688 货源（开放平台、网页读取都没接通时用）。

数据存在 offer 表、raw.manual = true，由 Web「选品中心 → 手工货源」录入。不是实时数据：
只给单品评估、找货源（按关键词）用，不参与周选品和在售款货源复查。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.types import OfferBrief, OfferDetail, OfferFilter, SupplierProfile
from app.models import Offer


def manual_offers(s: Session) -> list[Offer]:
    return [o for o in s.scalars(select(Offer).order_by(Offer.updated_at.desc())) if (o.raw or {}).get("manual")]


class ManualSourcing:
    channel = "manual"

    def __init__(self, session: Session):
        self.s = session

    def search_by_keyword(self, kw: str, filters: OfferFilter, limit: int) -> list[OfferBrief]:
        words = [w for w in kw.split() if w] or [kw]
        hits = [o for o in manual_offers(self.s) if all(w in o.title for w in words)
                and (not filters.dropship_only or o.dropship)
                and (filters.max_price_fen is None or o.price_fen <= filters.max_price_fen)]
        return [OfferBrief(offer_id=o.id, title=o.title, price_fen=o.price_fen,
                           image_url=(o.image_urls or [""])[0]) for o in hits[:limit]]

    def search_by_image(self, image: Path, limit: int) -> list[OfferBrief]:
        return []  # 手工数据没有以图搜

    def offer_detail(self, offer_id: str) -> OfferDetail:
        o = self.s.get(Offer, offer_id)
        if o is None or not (o.raw or {}).get("manual"):
            raise LookupError(f"货源 {offer_id} 没有手工录入，请先在 Web「选品中心 → 手工货源」录入")
        return OfferDetail.model_validate({k: v for k, v in o.raw.items() if k in OfferDetail.model_fields})

    def supplier_profile(self, member_id: str) -> SupplierProfile:
        raise LookupError("手工货源没有供应商档案")
