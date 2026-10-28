"""Seedream 生图：请求里带参考图和「商品不变」约束，结果存到素材目录；鉴权失败时健康检查报错。"""
import base64
import json

import httpx
import pytest

from app.adapters.image_gen.seedream import ImageGenError, SeedreamImageGen

PNG = base64.b64encode(b"fake-image-bytes").decode()


def _gen(tmp_path, handler):
    client = httpx.Client(base_url="https://ark.test/api/v3", transport=httpx.MockTransport(handler))
    return SeedreamImageGen("k", "seedream-x", out_dir=tmp_path / "generated", assets_dir=tmp_path, client=client)


def test_replace_background_sends_reference_and_saves(tmp_path):
    (tmp_path / "abc.jpg").write_bytes(b"src")
    seen = {}

    def handler(req: httpx.Request):
        seen.update(json.loads(req.content), auth=req.headers["Authorization"])
        return httpx.Response(200, json={"data": [{"b64_json": PNG}]})

    out = _gen(tmp_path, handler).replace_background("abc.jpg", "卧室床头柜")  # 只给文件名，到素材目录里找
    assert out.parent == tmp_path / "generated" and out.read_bytes() == b"fake-image-bytes"
    assert seen["auth"] == "Bearer k" and seen["model"] == "seedream-x" and seen["watermark"] is False
    assert seen["image"].startswith("data:image/jpeg;base64,") and "卧室床头柜" in seen["prompt"]
    assert "商品本体完全不变" in seen["prompt"]


def test_url_and_scene_reference(tmp_path):
    (tmp_path / "ref.png").write_bytes(b"ref")
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"data": [{"b64_json": PNG}]})

    out = _gen(tmp_path, handler).compose_scene("https://cbu01.alicdn.com/img/ibank/O1CN01x.jpg", "书桌", tmp_path / "ref.png")
    assert seen["image"][0].startswith("https://") and seen["image"][1].startswith("data:image/png;base64,")
    assert out.name.startswith("O1CN01x_scene_")


def test_errors(tmp_path):
    gen = _gen(tmp_path, lambda req: httpx.Response(401, json={"error": {"message": "invalid key"}}))
    with pytest.raises(ImageGenError, match="invalid key"):
        gen.health_check()
    with pytest.raises(FileNotFoundError):
        gen.remove_background("missing.jpg")
    with pytest.raises(ImageGenError, match="没开通"):
        _gen(tmp_path, lambda req: httpx.Response(404, json={"error": {"message": "not found"}})).health_check()
    # 参数错误（400）说明鉴权已通过，健康检查不算失败
    _gen(tmp_path, lambda req: httpx.Response(400, json={"error": {"message": "prompt required"}})).health_check()
