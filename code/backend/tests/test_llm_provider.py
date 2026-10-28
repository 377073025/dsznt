"""模型服务商切换：兼容 Anthropic 格式的服务商（DeepSeek）不支持结构化输出和 Batch 时的处理。"""
from __future__ import annotations

from app.adapters.llm.anthropic_client import AnthropicLLM, _params
from app.adapters.llm.base import LLMRequest
from app.config import get_settings
from app.secrets import set_secret
from app.services import build_services

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}


def _req(**kw):
    return LLMRequest(agent="exec.copy", model="deepseek-flash", effort="low", system="你是文案。",
                      messages=[{"role": "user", "content": "写标题"}], output_schema=SCHEMA, **kw)


def test_structured_output_uses_format():
    p = _params(_req())
    assert p["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert p["system"][0]["text"] == "你是文案。"


def test_without_structured_output_schema_goes_to_system_prompt():
    p = _params(_req(), structured_output=False)
    assert p["output_config"] == {"effort": "low"}
    assert '"title"' in p["system"][0]["text"] and "JSON Schema" in p["system"][0]["text"]


def test_real_mode_builds_client_from_settings(monkeypatch):
    monkeypatch.setenv("DSZNT_ADAPTER_MODE", "real")
    conf = get_settings().llm
    set_secret(conf["key_ref"], "sk-test-provider")
    llm = build_services().llm
    assert isinstance(llm, AnthropicLLM)
    assert str(llm.client.base_url).rstrip("/") == (conf["base_url"] or "https://api.anthropic.com").rstrip("/")
    assert llm.structured_output == conf["structured_output"] and llm.supports_batch == conf["batch"]
    assert llm.health_model == get_settings().model_for("light")["model"]


def test_no_batch_provider_refuses_batch():
    llm = AnthropicLLM("sk-test", "deepseek-flash", base_url="https://api.deepseek.com/anthropic", supports_batch=False)
    try:
        llm.submit_batch({"a": _req()})
    except RuntimeError as e:
        assert "Batch" in str(e)
    else:
        raise AssertionError("应拒绝 Batch")
