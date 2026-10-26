import pytest

from app.db import session_scope
from app.models import Task
from app.tools.registry import ToolContext, tools_for
from app.tools.write import WriteNotAllowed, ensure_write_allowed


def test_readonly_agents_cannot_register_write_tools():
    with pytest.raises(PermissionError):
        tools_for("ops.diagnose", ["pdd_update_goods"])
    with pytest.raises(PermissionError):
        tools_for("selection.recommend", ["create_task"])
    with pytest.raises(PermissionError):
        tools_for("ceo.dispatch", ["push_to_dropship_tool"])  # CEO 不能直接写店铺
    assert tools_for("exec.writer", ["pdd_update_goods"])


@pytest.mark.parametrize("status,confirmed_by", [
    ("produced", ""), ("awaiting_write_confirm", ""), ("writing", ""), ("writing", "ou_operator"),
])
def test_shop_write_denied_without_owner_confirmation(env, status, confirmed_by):
    services, _ = env
    with session_scope() as s:
        t = Task(ref_type="suggestion", ref_id=1, executor="image", spec={"goods_id": "g_haiyan"},
                 needs_write=True, status=status, write_confirmed_by=confirmed_by)
        s.add(t)
        s.flush()
        with pytest.raises(WriteNotAllowed):
            ensure_write_allowed(ToolContext(s, services, "exec.writer"), t.id)
    assert services._pdd_writer.calls == []
