"""Tests for mancp CLI commands."""

import json
from pathlib import Path
from unittest.mock import patch

from mancp.cli import cmd_init, cmd_list


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_cmd_init_imports_mcps(tmp_path, capsys):
    store_file = tmp_path / "store" / "mcps.json"
    claude_json = tmp_path / ".claude.json"
    _write_json(claude_json, {
        "mcpServers": {
            "server-a": {"command": "npx", "args": ["-y", "a"]},
            "server-b": {"command": "node", "args": ["b"]},
        },
    })

    with (
        patch("mancp.cli.load_store", return_value={}),
        patch("mancp.cli.collect_all_from_claude_json", return_value={
            "server-a": {"command": "npx", "args": ["-y", "a"]},
            "server-b": {"command": "node", "args": ["b"]},
        }),
        patch("mancp.cli.save_store") as mock_save,
    ):
        cmd_init()

    out = capsys.readouterr().out
    assert "2 MCP(s)" in out
    assert "server-a" in out
    assert mock_save.called


def test_cmd_init_skips_existing(capsys):
    with (
        patch("mancp.cli.load_store", return_value={"existing": {"command": "x"}}),
        patch("mancp.cli.collect_all_from_claude_json", return_value={
            "existing": {"command": "x"},
            "new-one": {"command": "y"},
        }),
        patch("mancp.cli.save_store"),
    ):
        cmd_init()

    out = capsys.readouterr().out
    assert "1 MCP(s)" in out
    assert "skipped 1" in out


def test_cmd_init_nothing_found(capsys):
    with (
        patch("mancp.cli.load_store", return_value={}),
        patch("mancp.cli.collect_all_from_claude_json", return_value={}),
    ):
        cmd_init()

    out = capsys.readouterr().out
    assert "No MCP servers found" in out


def test_cmd_list_empty(capsys):
    with patch("mancp.cli.load_store", return_value={}):
        cmd_list()

    out = capsys.readouterr().out
    assert "No MCPs in store" in out


def test_cmd_list_shows_entries(capsys, tmp_path):
    store = {
        "my-server": {"command": "npx", "args": ["-y", "my-server"]},
    }
    with (
        patch("mancp.cli.load_store", return_value=store),
        patch("mancp.cli.get_user_scope_mcps", return_value={"my-server"}),
        patch("mancp.cli.get_project_scope_mcps", return_value=set()),
        patch("mancp.cli.STORE_FILE", tmp_path / "mcps.json"),
    ):
        cmd_list()

    out = capsys.readouterr().out
    assert "my-server" in out
    assert "[U]" in out
    assert "1 entries" in out
