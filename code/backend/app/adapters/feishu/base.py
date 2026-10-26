from __future__ import annotations

from pathlib import Path
from typing import Protocol


class FeishuAdapter(Protocol):
    def send_card(self, chat_id: str, card: dict) -> str: ...  # 返回 message_id
    def update_card(self, token: str, card: dict) -> None: ...  # 延时更新卡片
    def send_text(self, chat_id: str, text: str) -> str: ...
    def download_file(self, message_id: str, file_key: str) -> Path: ...
