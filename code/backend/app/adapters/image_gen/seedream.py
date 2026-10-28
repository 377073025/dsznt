"""火山方舟 Seedream 生图（R11）：拿实物图当参考改背景 / 合成场景，商品本体保持不变。

接口：POST {base_url}/images/generations，Bearer 鉴权；参考图传 URL 或 data URI，返回 base64 存到素材目录。
"""
from __future__ import annotations

import base64
import mimetypes
import re
import time
from pathlib import Path

import httpx

# 每次都带上：平台规则要求主图和实物一致，只能动背景和场景
_KEEP = "保持图中商品本体完全不变：形状、颜色、材质、图案、文字、数量都不要改，不要增加或删除商品部件。"


class ImageGenError(RuntimeError):
    pass


class SeedreamImageGen:
    def __init__(self, api_key: str, model: str, out_dir: Path, base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
                 size: str = "2K", assets_dir: Path | None = None, client: httpx.Client | None = None):
        self.model, self.size, self.out_dir, self.assets_dir = model, size, Path(out_dir), assets_dir
        self.client = client or httpx.Client(base_url=base_url, timeout=180)
        self.client.headers["Authorization"] = f"Bearer {api_key}"

    def remove_background(self, img: Path | str) -> Path:
        return self._edit(img, "把背景换成纯白色（#FFFFFF），商品居中，四周留白，柔和自然的阴影，电商白底主图。", "nobg")

    def replace_background(self, img: Path | str, prompt: str) -> Path:
        return self._edit(img, f"只替换背景：{prompt}。光线和阴影与新背景协调，电商主图构图。", "bg")

    def compose_scene(self, product_img: Path | str, prompt: str, ref: Path | None) -> Path:
        extra = "第二张图只是场景风格参考，不要把其中的物品放进画面。" if ref else ""
        return self._edit(product_img, f"把商品放进使用场景：{prompt}。{extra}真实摄影质感，商品是画面主体。", "scene",
                          ref=ref)

    def health_check(self) -> None:
        """不生成图片（要扣费）：发一个缺参数的请求，只看鉴权是否通过。"""
        r = self.client.post("/images/generations", json={"model": self.model, "prompt": ""})
        if r.status_code in (401, 403):
            raise ImageGenError(f"火山方舟拒绝了 API Key（{r.status_code}）：{_error(r)}")
        if r.status_code == 404:
            raise ImageGenError(f"模型 {self.model} 没开通（或 settings.yaml 里的模型 ID 写错）：{_error(r)}")

    def _edit(self, img: Path | str, instruction: str, tag: str, ref: Path | None = None) -> Path:
        images = [self._image_input(img)] + ([self._image_input(ref)] if ref else [])
        r = self.client.post("/images/generations", json={
            "model": self.model, "prompt": f"{instruction}{_KEEP}", "image": images if len(images) > 1 else images[0],
            "size": self.size, "response_format": "b64_json", "watermark": False,
        })
        if r.status_code != 200:
            raise ImageGenError(f"生图失败（{r.status_code}）：{_error(r)}")
        data = r.json()["data"][0]
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = self.out_dir / f"{_stem(img)}_{tag}_{time.strftime('%Y%m%d%H%M%S')}.jpg"
        out.write_bytes(base64.b64decode(data["b64_json"]))
        return out

    def _image_input(self, img: Path | str) -> str:
        """URL 原样传；本地文件转 data URI。只给文件名时到素材目录里找。"""
        s = str(img)
        if s.startswith(("http://", "https://")):
            return s
        path = Path(img)
        if not path.exists() and self.assets_dir is not None and (self.assets_dir / path.name).exists():
            path = self.assets_dir / path.name
        if not path.exists():
            raise FileNotFoundError(f"找不到原图：{img}（先把商品主图放进素材目录，或给图片链接）")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _stem(img: Path | str) -> str:
    s = str(img)
    name = s.rsplit("/", 1)[-1] if s.startswith("http") else Path(s).name
    return re.sub(r"[^\w\-]+", "_", Path(name.split("?")[0]).stem)[:40].strip("_") or "image"


def _error(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["message"][:200]
    except Exception:
        return r.text[:200]
