"""代发助手王数据解析（纯函数）：输入是页面自己请求回来的 JSON（经 1688 开放平台网关转发到爱用的接口）。

- 店铺：Distributeproxy/getProxyShopInfo（authorization_status=1 为已授权，expire_time 授权到期）
- 订单计数：Orderreturn/getOrderMessages?shopId=…（待采购 waittobuy、待发货、已发货、退款中）
- 已关联货源的商品：Orderreturn/getItemList（num_iid 店铺商品，origin_num_iid 1688 货源，status_1688 货源状态）
- 在售商品的关联状态：ItemSync/getItemList（association_item_infos / distribute_association_item 为空即未关联）
- 软件到期：m1688/get_service_end
"""
from __future__ import annotations

from typing import Any


class DaifaDataError(RuntimeError):
    pass


def _result(resp: dict[str, Any]) -> Any:
    code = resp.get("code")
    if code is not None and str(code) != "200":
        raise DaifaDataError(f"代发助手王返回错误：{resp.get('message') or resp.get('msg') or code}")
    return resp.get("result", resp.get("data", resp))


def pdd_shop(resp: dict[str, Any]) -> dict[str, Any]:
    for s in _result(resp) or []:
        if s.get("shop_type") == "pdd":
            return {"id": s["id"], "name": s.get("shop_name", ""), "authorized": str(s.get("authorization_status")) == "1",
                    "auth_expire": (s.get("expire_time") or "")[:10]}
    raise DaifaDataError("代发助手王里没有绑定拼多多店铺")


def order_counts(resp: dict[str, Any]) -> dict[str, int]:
    r = _result(resp)
    return {"wait_purchase": int(r.get("waittobuy") or 0), "wait_send": int(r.get("waittosend") or 0),
            "sent": int(r.get("sellersended") or 0), "refunding": int(r.get("buyerrefunded") or 0)}


def linked_goods(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """已在代发助手王里关联了 1688 货源的店铺商品；货源 ID 统一成系统里的「1688_数字」。"""
    return [{"goods_id": r["num_iid"], "offer_id": f"1688_{r['origin_num_iid']}", "title": r.get("title", ""),
             "source_status": r.get("status_1688", ""), "source_price_fen": round(float(r.get("origin_price") or 0) * 100)}
            for r in _result(resp) or [] if str(r.get("relation_is_cancel", "0")) == "0"]


def unlinked_onsale(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """在售但没关联货源的商品（来了订单不会自动采购）；0.01 元的补差价链接不算。"""
    out = []
    for r in _result(resp) or []:
        if r.get("association_item_infos") or r.get("distribute_association_item"):
            continue
        price = float(r.get("price") or 0)
        if price < 1:
            continue
        out.append({"goods_id": r["productid"], "name": r.get("name", ""), "price_fen": round(price * 100),
                    "sku_count": len(r.get("skuInfos") or [])})
    return out


def service_end(resp: dict[str, Any]) -> str:
    _result(resp)
    return (resp.get("gmtServiceEnd") or "")[:10]
