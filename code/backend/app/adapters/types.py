"""适配器之间传递的标准结构。各来源（API / 网页 / 人工导入）都先转成这些结构再入库。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field


class GoodsInfo(BaseModel):
    goods_id: str
    name: str
    series: str = ""
    title: str
    price_fen: int
    main_image_hash: str
    skus: list[dict] = Field(default_factory=list)


class GoodsTraffic(BaseModel):
    goods_id: str
    day: date
    visitors: int
    add_to_cart: int
    orders: int
    gmv_fen: int = 0
    refunds: int = 0


class OrderStats(BaseModel):
    day: date
    orders: int
    gmv_fen: int
    refunds: int


class PromotionStat(BaseModel):
    goods_id: str
    day: date
    spend_fen: int
    impressions: int
    clicks: int


class KeywordStat(BaseModel):
    keyword: str
    heat: float
    trend: float  # 近 4 周变化率
    competition: float  # 0–1，越大越激烈


class RankItem(BaseModel):
    rank: int
    title: str
    price_fen: int
    sales_30d: int
    image_url: str = ""


class CompetitorGoods(BaseModel):
    url: str
    title: str
    price_fen: int
    sales_30d: int
    image_url: str = ""


class OfferFilter(BaseModel):
    dropship_only: bool = True
    max_price_fen: int | None = None


class OfferBrief(BaseModel):
    offer_id: str
    title: str
    price_fen: int
    image_url: str = ""


class OfferDetail(BaseModel):
    offer_id: str
    title: str
    category: str = ""
    price_fen: int
    shipping_fen: int = 0
    dropship: bool
    stock: int
    ship_48h_rate: float
    repurchase_rate: float
    return_rate: float
    category_return_rate: float = 0.0
    supplier_years: int
    weight_kg: float = 0.0
    longest_side_cm: float = 0.0
    image_urls: list[str] = Field(default_factory=list)


class SupplierProfile(BaseModel):
    member_id: str
    years: int
    ship_48h_rate: float
    repurchase_rate: float


class AutoOrderStatus(BaseModel):
    offer_id: str
    supported: bool | None  # None = 未知


class DropshipOrder(BaseModel):
    order_id: str
    offer_id: str
    status: str


class ListingPackage(BaseModel):
    title: str
    main_images: list[str]
    detail_images: list[str] = Field(default_factory=list)
    skus: list[dict] = Field(default_factory=list)
    price_fen: int
    source_offer_url: str = ""
    path: str = ""


class GoodsPatch(BaseModel):
    title: str | None = None
    main_image: str | None = None
    price_fen: int | None = None


class WriteResult(BaseModel):
    ok: bool
    ref: str = ""
    message: str = ""


class ImagePath(BaseModel):
    path: Path
