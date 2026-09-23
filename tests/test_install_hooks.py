import importlib.util

from conftest import REPO

spec = importlib.util.spec_from_file_location("install_hooks", REPO / "scripts" / "install_hooks.py")
install_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_hooks)

EXISTING = {
    "model": "x",
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/guard.sh"}]}]},
}


def test_merge_preserves_existing_and_is_idempotent():
    once = install_hooks.merge(EXISTING, "agent-selection", uninstall=False)
    twice = install_hooks.merge(once, "agent-selection", uninstall=False)
    assert once == twice
    assert once["model"] == "x"
    assert once["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert once["hooks"]["PreToolUse"][1]["matcher"] == "Agent|Task|Skill"
    assert "hook user-prompt --gate agent-selection" in once["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]


def test_uninstall_restores_the_original():
    once = install_hooks.merge(EXISTING, "agent-selection", uninstall=False)
    assert install_hooks.merge(once, "agent-selection", uninstall=True) == EXISTING
    assert install_hooks.merge({}, "agent-selection", uninstall=True) == {}
