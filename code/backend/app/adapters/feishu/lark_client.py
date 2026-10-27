"""飞书真实实现（官方 SDK lark-oapi）。需要 R04、R05：自建应用的 App ID / App Secret。

权限（以开发者后台名称为准）：以应用身份发消息、读取与更新消息、获取消息中的资源文件。
"""
from __future__ import annotations

import json
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.auth.v3 import InternalTenantAccessTokenRequest, InternalTenantAccessTokenRequestBody
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
)


class FeishuError(Exception):
    pass


def _check(resp, what: str):
    if not resp.success():
        raise FeishuError(f"{what} 失败：code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
    return resp


class LarkFeishu:
    def __init__(self, app_id: str, app_secret: str, download_dir: Path):
        self.app_id, self.app_secret = app_id, app_secret
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(lark.LogLevel.WARNING).build()
        self.download_dir = download_dir

    def _send(self, receive_id: str, msg_type: str, content: dict, id_type: str = "chat_id") -> str:
        req = (CreateMessageRequest.builder().receive_id_type(id_type)
               .request_body(CreateMessageRequestBody.builder().receive_id(receive_id).msg_type(msg_type)
                             .content(json.dumps(content, ensure_ascii=False)).build())
               .build())
        return _check(self.client.im.v1.message.create(req), "发送消息").data.message_id

    def send_card(self, chat_id: str, card: dict) -> str:
        return self._send(chat_id, "interactive", card)

    def send_text(self, chat_id: str, text: str) -> str:
        return self._send(chat_id, "text", {"text": text})

    def send_card_to_user(self, open_id: str, card: dict) -> str:
        return self._send(open_id, "interactive", card, "open_id")

    def send_text_to_user(self, open_id: str, text: str) -> str:
        return self._send(open_id, "text", {"text": text}, "open_id")

    def update_card(self, token: str, card: dict) -> None:
        """token 为 message_id：用「更新应用发送的消息卡片」接口，卡片需开启 update_multi。"""
        req = (PatchMessageRequest.builder().message_id(token)
               .request_body(PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build())
               .build())
        _check(self.client.im.v1.message.patch(req), "更新卡片")

    def download_file(self, message_id: str, file_key: str) -> Path:
        req = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type("file").build()
        resp = _check(self.client.im.v1.message_resource.get(req), "下载文件")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        path = self.download_dir / f"{message_id}_{Path(resp.file_name or file_key).name}"
        path.write_bytes(resp.file.read())
        return path

    def health_check(self) -> None:
        req = (InternalTenantAccessTokenRequest.builder()
               .request_body(InternalTenantAccessTokenRequestBody.builder().app_id(self.app_id).app_secret(self.app_secret).build())
               .build())
        _check(self.client.auth.v3.tenant_access_token.internal(req), "获取 tenant_access_token")
