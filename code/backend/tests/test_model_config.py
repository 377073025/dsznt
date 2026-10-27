"""按智能体单独配置模型（settings.yaml 的 agent_models）。"""
from __future__ import annotations

import copy

import pytest

from app.config import Settings, get_settings


def _settings(agent_models):
    raw = copy.deepcopy(get_settings().raw)
    raw["agent_models"] = agent_models
    return Settings(raw, {}, {})


def test_default_role_when_not_configured():
    st = _settings(None)
    assert st.model_for_agent("ops.diagnose", "main") == st.model_for("main")


def test_full_name_beats_agent_name():
    st = _settings({"ceo": "light", "ceo.daily": "main"})
    assert st.model_for_agent("ceo.daily", "ceo")["model"] == st.model_for("main")["model"]
    assert st.model_for_agent("ceo.accept", "ceo")["model"] == st.model_for("light")["model"]


def test_override_only_effort_keeps_default_model():
    st = _settings({"ops": {"effort": "high"}})
    m = st.model_for_agent("ops.diagnose", "main")
    assert m == {"model": st.model_for("main")["model"], "effort": "high"}


def test_explicit_model():
    st = _settings({"selection.same_product": {"model": "claude-sonnet-5-5", "effort": "low"}})
    assert st.model_for_agent("selection.same_product", "light") == {"model": "claude-sonnet-5-5", "effort": "low"}


@pytest.mark.parametrize("bad", [{"ceo": "nope"}, {"ceo.chat": {"model": "unknown-model"}}])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError, match="模型配置有误"):
        _settings(bad).validate_models()


def test_shipped_config_is_valid():
    get_settings().validate_models()
