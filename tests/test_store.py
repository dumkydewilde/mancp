"""Tests for mancp.store module."""

import json
from pathlib import Path

from mancp.store import (
    apply_changes,
    apply_plugin_changes,
    categorize_readonly,
    collect_all_from_claude_json,
    collect_readonly_mcps,
    count_mcp_tools,
    get_plugins,
    get_project_scope_mcps,
    get_user_scope_mcps,
    load_store,
    mask_secrets,
    remove_mcp_everywhere,
    save_store,
    set_plugin_enabled,
    tool_count_for,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ── load/save store ──────────────────────────────────────────────────────────


def test_load_store_missing(tmp_path):
    assert load_store(tmp_path / "nope.json") == {}


def test_load_save_roundtrip(tmp_path):
    f = tmp_path / "store.json"
    data = {"my-mcp": {"command": "npx", "args": ["-y", "some-server"]}}
    save_store(data, f)
    assert load_store(f) == data


# ── collect_all_from_claude_json ─────────────────────────────────────────────


def test_collect_global_and_project_mcps(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {
        "mcpServers": {
            "global-mcp": {"command": "npx", "args": ["-y", "g"]},
        },
        "projects": {
            "/some/path": {
                "mcpServers": {
                    "project-mcp": {"command": "node", "args": ["p"]},
                }
            }
        },
    })
    result = collect_all_from_claude_json(claude)
    assert "global-mcp" in result
    assert "project-mcp" in result


def test_collect_global_takes_precedence(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {
        "mcpServers": {
            "dup": {"command": "global-cmd"},
        },
        "projects": {
            "/x": {
                "mcpServers": {
                    "dup": {"command": "project-cmd"},
                }
            }
        },
    })
    result = collect_all_from_claude_json(claude)
    assert result["dup"]["command"] == "global-cmd"


def test_collect_empty_claude_json(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {})
    assert collect_all_from_claude_json(claude) == {}


def test_collect_missing_file(tmp_path):
    assert collect_all_from_claude_json(tmp_path / "missing.json") == {}


# ── collect_readonly_mcps ────────────────────────────────────────────────────


def test_collect_readonly_needs_auth(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    _write_json(claude_dir / "mcp-needs-auth-cache.json", {
        "claude.ai Notion": {"timestamp": 123},
        "claude.ai Clay": {"timestamp": 456},
    })
    result = collect_readonly_mcps(claude_dir)
    assert result["claude.ai Notion"] == "needs auth"
    assert result["claude.ai Clay"] == "needs auth"


def test_collect_readonly_connected_from_permissions(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    _write_json(claude_dir / "settings.local.json", {
        "permissions": {
            "allow": [
                "mcp__claude_ai_Slack__slack_read_channel",
                "mcp__claude_ai_Gmail__gmail_search",
            ],
        },
    })
    result = collect_readonly_mcps(claude_dir)
    assert result["claude.ai Slack"] == "connected"
    assert result["claude.ai Gmail"] == "connected"


def test_collect_readonly_needs_auth_takes_precedence(tmp_path):
    """If in needs-auth cache, show 'needs auth' even if permissions exist."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    _write_json(claude_dir / "mcp-needs-auth-cache.json", {
        "claude.ai Slack": {"timestamp": 123},
    })
    _write_json(claude_dir / "settings.local.json", {
        "permissions": {
            "allow": ["mcp__claude_ai_Slack__slack_read_channel"],
        },
    })
    result = collect_readonly_mcps(claude_dir)
    assert result["claude.ai Slack"] == "needs auth"


def test_collect_readonly_plugins(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    _write_json(claude_dir / "settings.json", {
        "enabledPlugins": {
            "linear@claude-plugins-official": True,
            "figma@claude-plugins-official": False,
        },
    })
    result = collect_readonly_mcps(claude_dir)
    assert result["plugin:linear"] == "plugin"
    assert result["plugin:figma"] == "plugin (disabled)"


def test_collect_readonly_user_mcp_json(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    _write_json(claude_dir / ".mcp.json", {
        "mcpServers": {"my-server": {"command": "node"}},
    })
    result = collect_readonly_mcps(claude_dir)
    assert result["my-server"] == "~/.claude/.mcp.json"


def test_collect_readonly_empty_dir(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    assert collect_readonly_mcps(claude_dir) == {}


# ── categorize_readonly ─────────────────────────────────────────────────────


def test_categorize_readonly_splits_by_type():
    readonly = {
        "claude.ai Slack": "connected",
        "claude.ai Gmail": "needs auth",
        "plugin:linear": "plugin",
        "plugin:figma": "plugin (disabled)",
        "my-server": "~/.claude/.mcp.json",
    }
    cats = categorize_readonly(readonly)
    assert set(cats["cloud"].keys()) == {"claude.ai Slack", "claude.ai Gmail"}
    assert set(cats["plugin"].keys()) == {"plugin:linear", "plugin:figma"}
    assert set(cats["user_mcp"].keys()) == {"my-server"}


def test_categorize_readonly_omits_empty():
    readonly = {"claude.ai Slack": "connected"}
    cats = categorize_readonly(readonly)
    assert "cloud" in cats
    assert "plugin" not in cats
    assert "user_mcp" not in cats


# ── count_mcp_tools ─────────────────────────────────────────────────────────


def test_count_mcp_tools(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    _write_json(claude_dir / "settings.local.json", {
        "permissions": {
            "allow": [
                "mcp__github__get_me",
                "mcp__github__list_pull_requests",
                "mcp__github__search_code",
                "mcp__slack__read_channel",
            ],
        },
    })
    counts = count_mcp_tools(claude_dir)
    assert counts["github"] == 3
    assert counts["slack"] == 1


def test_count_mcp_tools_empty_dir(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    assert count_mcp_tools(claude_dir) == {}


# ── token estimates ──────────────────────────────────────────────────────────


def test_tool_count_for():
    counts = {"my_mcp": 5, "other": 10}
    assert tool_count_for("my_mcp", counts) == 5
    # hyphen-to-underscore fallback
    assert tool_count_for("my-mcp", counts) == 5
    assert tool_count_for("unknown", counts) == 0


# ── scope helpers ────────────────────────────────────────────────────────────


def test_get_user_scope_mcps(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {"a": {}, "b": {}}})
    assert get_user_scope_mcps(claude) == {"a", "b"}


def test_get_project_scope_mcps(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"x": {}, "y": {}}})
    assert get_project_scope_mcps(tmp_path) == {"x", "y"}


def test_get_project_scope_mcps_no_file(tmp_path):
    assert get_project_scope_mcps(tmp_path) == set()


# ── mask_secrets ─────────────────────────────────────────────────────────────


def test_mask_secrets_masks_token():
    cfg = {"env": {"MOTHERDUCK_TOKEN": "abcdefghijklmnop"}}
    masked = mask_secrets(cfg)
    assert masked["env"]["MOTHERDUCK_TOKEN"] == "abcd...mnop"


def test_mask_secrets_short_value():
    cfg = {"env": {"API_KEY": "short"}}
    masked = mask_secrets(cfg)
    assert masked["env"]["API_KEY"] == "***"


def test_mask_secrets_leaves_non_secret():
    cfg = {"command": "npx", "args": ["-y", "server"]}
    assert mask_secrets(cfg) == cfg


def test_mask_secrets_nested_list():
    cfg = {"items": [{"secret": "abcdefghijklmnop"}]}
    masked = mask_secrets(cfg)
    assert masked["items"][0]["secret"] == "abcd...mnop"


# ── apply_changes ────────────────────────────────────────────────────────────


def test_apply_writes_user_and_project(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {}})

    store = {
        "mcp-a": {"command": "a"},
        "mcp-b": {"command": "b"},
        "mcp-c": {"command": "c"},
    }
    apply_changes(
        store,
        user_mcps={"mcp-a"},
        project_mcps={"mcp-b", "mcp-c"},
        cwd=tmp_path,
        claude_json=claude,
    )

    cdata = json.loads(claude.read_text())
    assert set(cdata["mcpServers"].keys()) == {"mcp-a"}

    proj = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(proj["mcpServers"].keys()) == {"mcp-b", "mcp-c"}


def test_apply_no_project_file_when_empty(tmp_path):
    """Should not create .mcp.json if no project MCPs and file doesn't exist."""
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {}})

    store = {"a": {"command": "x"}}
    apply_changes(store, user_mcps={"a"}, project_mcps=set(), cwd=tmp_path, claude_json=claude)

    assert not (tmp_path / ".mcp.json").exists()


def test_apply_updates_existing_project_file(tmp_path):
    """Should update .mcp.json if file already exists, even when clearing."""
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {}})
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"old": {"command": "x"}}})

    apply_changes(store={}, user_mcps=set(), project_mcps=set(), cwd=tmp_path, claude_json=claude)

    proj = json.loads((tmp_path / ".mcp.json").read_text())
    assert proj["mcpServers"] == {}


def test_apply_clears_removed_mcps(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {"old": {"command": "old"}}})
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"old-proj": {"command": "x"}}})

    store = {"new": {"command": "new"}}
    apply_changes(store, user_mcps=set(), project_mcps=set(), cwd=tmp_path, claude_json=claude)

    cdata = json.loads(claude.read_text())
    assert cdata["mcpServers"] == {}
    proj = json.loads((tmp_path / ".mcp.json").read_text())
    assert proj["mcpServers"] == {}


def test_apply_creates_backup(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {"x": {}}})

    apply_changes({}, set(), set(), tmp_path, claude_json=claude)

    backups = list(tmp_path.glob(".claude.json.bak.*"))
    assert len(backups) == 1


def test_apply_mcp_can_be_in_both_scopes(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {}})

    store = {"shared": {"command": "npx"}}
    apply_changes(
        store,
        user_mcps={"shared"},
        project_mcps={"shared"},
        cwd=tmp_path,
        claude_json=claude,
    )

    cdata = json.loads(claude.read_text())
    assert "shared" in cdata["mcpServers"]
    proj = json.loads((tmp_path / ".mcp.json").read_text())
    assert "shared" in proj["mcpServers"]


def test_apply_returns_status_message(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {}})

    store = {"a": {"command": "x"}}
    msg = apply_changes(store, {"a"}, set(), tmp_path, claude_json=claude)
    assert "U: 1" in msg


# ── plugin management ──────────────────────────────────────────────────────


def test_get_plugins(tmp_path):
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "enabledPlugins": {
            "linear@official": True,
            "figma@official": False,
        },
    })
    result = get_plugins(settings)
    assert result == {"linear@official": True, "figma@official": False}


def test_get_plugins_missing_file(tmp_path):
    assert get_plugins(tmp_path / "nope.json") == {}


def test_get_plugins_no_key(tmp_path):
    settings = tmp_path / "settings.json"
    _write_json(settings, {"permissions": {}})
    assert get_plugins(settings) == {}


def test_set_plugin_enabled(tmp_path):
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "enabledPlugins": {"linear@official": True},
    })
    set_plugin_enabled("linear@official", False, settings)
    data = json.loads(settings.read_text())
    assert data["enabledPlugins"]["linear@official"] is False


def test_set_plugin_enabled_adds_new(tmp_path):
    settings = tmp_path / "settings.json"
    _write_json(settings, {})
    set_plugin_enabled("new-plugin@test", True, settings)
    data = json.loads(settings.read_text())
    assert data["enabledPlugins"]["new-plugin@test"] is True


def test_apply_plugin_changes(tmp_path):
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "permissions": {"allow": []},
        "enabledPlugins": {"a@x": True, "b@x": True},
    })
    plugins = {"a@x": False, "b@x": True, "c@x": True}
    msg = apply_plugin_changes(plugins, settings)

    data = json.loads(settings.read_text())
    assert data["enabledPlugins"] == {"a@x": False, "b@x": True, "c@x": True}
    # Preserves other keys
    assert "permissions" in data
    assert "2/3 enabled" in msg


def test_apply_plugin_changes_preserves_settings(tmp_path):
    """Ensure non-plugin settings are preserved."""
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {"SessionStart": []},
        "enabledPlugins": {"old@x": True},
    })
    apply_plugin_changes({"new@x": True}, settings)
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"] == {"SessionStart": []}
    assert data["enabledPlugins"] == {"new@x": True}


# ── remove_mcp_everywhere ────────────────────────────────────────────────


def test_remove_mcp_from_global_and_project_scopes(tmp_path):
    claude = tmp_path / ".claude.json"
    _write_json(claude, {
        "mcpServers": {"todoist": {"url": "https://ai.todoist.net/mcp"}, "github": {"command": "gh"}},
        "projects": {
            "/some/path": {
                "mcpServers": {"todoist": {"url": "https://ai.todoist.net/mcp"}},
            },
            "/other/path": {
                "mcpServers": {"slack": {"command": "slack"}},
            },
        },
    })
    _write_json(tmp_path / ".mcp.json", {
        "mcpServers": {"todoist": {"url": "https://ai.todoist.net/mcp"}},
    })

    remove_mcp_everywhere("todoist", tmp_path, claude_json=claude)

    data = json.loads(claude.read_text())
    assert "todoist" not in data["mcpServers"]
    assert "github" in data["mcpServers"]
    assert "todoist" not in data["projects"]["/some/path"]["mcpServers"]
    assert "slack" in data["projects"]["/other/path"]["mcpServers"]

    proj = json.loads((tmp_path / ".mcp.json").read_text())
    assert "todoist" not in proj["mcpServers"]


def test_remove_mcp_no_mcp_json(tmp_path):
    """Should not crash if .mcp.json doesn't exist."""
    claude = tmp_path / ".claude.json"
    _write_json(claude, {"mcpServers": {"x": {"command": "x"}}})

    remove_mcp_everywhere("x", tmp_path, claude_json=claude)

    data = json.loads(claude.read_text())
    assert "x" not in data["mcpServers"]
    assert not (tmp_path / ".mcp.json").exists()
