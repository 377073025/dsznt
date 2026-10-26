from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ImageGenAdapter(Protocol):
    """生图服务。约束：不生成商品本体，只处理背景与场景。"""

    def remove_background(self, img: Path) -> Path: ...
    def replace_background(self, img: Path, prompt: str) -> Path: ...
    def compose_scene(self, product_img: Path, prompt: str, ref: Path | None) -> Path: ...
