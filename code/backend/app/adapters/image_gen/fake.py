from __future__ import annotations

from pathlib import Path


class FakeImageGen:
    """返回带后缀的新路径，不真正生成图片。"""

    def remove_background(self, img: Path) -> Path:
        return img.with_name(f"{img.stem}_nobg{img.suffix}")

    def replace_background(self, img: Path, prompt: str) -> Path:
        return img.with_name(f"{img.stem}_bg{img.suffix}")

    def compose_scene(self, product_img: Path, prompt: str, ref: Path | None) -> Path:
        return product_img.with_name(f"{product_img.stem}_scene{product_img.suffix}")
