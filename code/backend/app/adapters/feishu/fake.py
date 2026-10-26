from __future__ import annotations

from pathlib import Path


class FakeFeishu:
    """把发出的消息记在内存里，测试和演示时查看。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.updated: list[dict] = []
        self.files: dict[str, Path] = {}

    def send_card(self, chat_id: str, card: dict) -> str:
        mid = f"om_fake_{len(self.sent) + 1}"
        self.sent.append({"message_id": mid, "chat_id": chat_id, "card": card})
        return mid

    def update_card(self, token: str, card: dict) -> None:
        self.updated.append({"token": token, "card": card})

    def send_text(self, chat_id: str, text: str) -> str:
        mid = f"om_fake_{len(self.sent) + 1}"
        self.sent.append({"message_id": mid, "chat_id": chat_id, "text": text})
        return mid

    def download_file(self, message_id: str, file_key: str) -> Path:
        return self.files[file_key]
