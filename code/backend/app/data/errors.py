"""数据层异常。风控类（验证码、登录失效、访问受限）会暂停整个平台，其余只让当前通道失败、降到下一个通道。"""
from __future__ import annotations


class FetchError(Exception):
    """采集失败的基类。"""


class CaptchaRequired(FetchError):
    """出现验证码 / 滑块：平台暂停，人工在专用浏览器里处理后恢复。"""


class LoginExpired(FetchError):
    """登录失效：平台暂停，人工重新扫码登录后恢复。"""


class RiskBlocked(FetchError):
    """访问受限等风控提示：熔断 ≥ 24 小时，只能店主确认恢复。"""


class ForbiddenAction(FetchError):
    """脚本或 AI 认路试图执行白名单以外的动作（点击写操作按钮、跳到其他域名、未知动作）。"""


class StepFailed(FetchError):
    """脚本某一步失败：元素找不到、结果为空、列名对不上。可以交给 AI 认路。"""

    def __init__(self, message: str, step_index: int = -1):
        super().__init__(message)
        self.step_index = step_index


class InvalidData(FetchError):
    """取到的数据没通过校验（缺字段、数字不合理），不入库。"""


class DailyLimitReached(FetchError):
    """平台当日页面数已用完。"""
