"""Tests for mancp.registry module."""

import json
from unittest.mock import patch, MagicMock

from mancp.registry import (
    MCPServerResult,
    is_in_store,
    search_github_mcp_org,
    search_mcp_registry,
    search_all,
)


def _fake_github_response(repos: list[dict]) -> bytes:
    """Build a fake GitHub search API response."""
    return json.dumps({
        "total_count": len(repos),
        "items": repos,
    }).encode()


def _fake_registry_response(servers: list[dict]) -> bytes:
    """Build a fake MCP registry API response."""
    return json.dumps({
        "servers": servers,
        "metadata": {"count": len(servers)},
    }).encode()


class TestMCPServerResult:
    def test_to_mcp_config_registry(self):
        r = MCPServerResult(
            name="filesystem",
            package="@modelcontextprotocol/server-filesystem",
            description="File system MCP",
            source="registry",
        )
        cfg = r.to_mcp_config()
        assert cfg == {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        }

    def test_to_mcp_config_github(self):
        r = MCPServerResult(
            name="servers",
            package="modelcontextprotocol/servers",
            description="MCP Servers",
            source="github_mcp",
        )
        cfg = r.to_mcp_config()
        assert cfg["command"] == "npx"

    def test_display_line(self):
        r = MCPServerResult(
            name="test-server",
            package="test-pkg",
            description="A test server",
            source="registry",
            stars=42,
        )
        line = r.display_line()
        assert "test-server" in line
        assert "42*" in line


class TestSearchGithubMcpOrg:
    def test_parses_results(self):
        fake_data = _fake_github_response([
            {
                "name": "servers",
                "full_name": "modelcontextprotocol/servers",
                "description": "Model Context Protocol Servers",
                "topics": ["mcp"],
                "html_url": "https://github.com/modelcontextprotocol/servers",
                "stargazers_count": 80000,
            },
        ])
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mancp.registry.urllib.request.urlopen", return_value=mock_resp):
            results = search_github_mcp_org("server")

        assert len(results) == 1
        assert results[0].name == "servers"
        assert results[0].source == "github_mcp"
        assert results[0].stars == 80000

    def test_strips_server_prefix(self):
        fake_data = _fake_github_response([
            {
                "name": "server-filesystem",
                "full_name": "modelcontextprotocol/server-filesystem",
                "description": "FS server",
                "topics": [],
                "html_url": "https://github.com/modelcontextprotocol/server-filesystem",
                "stargazers_count": 100,
            },
        ])
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mancp.registry.urllib.request.urlopen", return_value=mock_resp):
            results = search_github_mcp_org("filesystem")

        assert results[0].name == "filesystem"

    def test_handles_network_error(self):
        with patch("mancp.registry.urllib.request.urlopen", side_effect=TimeoutError):
            results = search_github_mcp_org("test")
        assert results == []


class TestSearchMcpRegistry:
    def test_parses_results(self):
        fake_data = _fake_registry_response([
            {
                "server": {
                    "name": "io.github.user/mcp-filesystem",
                    "description": "Filesystem server",
                    "version": "1.0.0",
                    "packages": [
                        {
                            "registryType": "npm",
                            "identifier": "@user/mcp-filesystem",
                            "version": "1.0.0",
                            "transport": {"type": "stdio"},
                        }
                    ],
                },
                "_meta": {
                    "io.modelcontextprotocol.registry/official": {
                        "status": "active",
                        "isLatest": True,
                    }
                },
            }
        ])
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mancp.registry.urllib.request.urlopen", return_value=mock_resp):
            results = search_mcp_registry("filesystem")

        assert len(results) == 1
        assert results[0].source == "registry"
        assert results[0].package == "@user/mcp-filesystem"
        assert results[0].version == "1.0.0"

    def test_handles_network_error(self):
        with patch("mancp.registry.urllib.request.urlopen", side_effect=TimeoutError):
            results = search_mcp_registry("test")
        assert results == []


class TestSearchAll:
    def test_deduplicates(self):
        gh_result = MCPServerResult(
            name="filesystem", package="modelcontextprotocol/server-filesystem",
            description="FS", source="github_mcp",
        )
        reg_result = MCPServerResult(
            name="filesystem", package="@mcp/server-filesystem",
            description="FS", source="registry",
        )
        with (
            patch("mancp.registry.search_github_mcp_org", return_value=[gh_result]),
            patch("mancp.registry.search_mcp_registry", return_value=[reg_result]),
        ):
            results = search_all("filesystem")

        assert len(results) == 1
        assert results[0].source == "github_mcp"

    def test_merges_unique(self):
        gh_result = MCPServerResult(
            name="servers", package="modelcontextprotocol/servers",
            description="Servers", source="github_mcp",
        )
        reg_result = MCPServerResult(
            name="custom-fs", package="@user/custom-fs",
            description="Custom FS", source="registry",
        )
        with (
            patch("mancp.registry.search_github_mcp_org", return_value=[gh_result]),
            patch("mancp.registry.search_mcp_registry", return_value=[reg_result]),
        ):
            results = search_all("test")

        assert len(results) == 2


class TestIsInStore:
    def test_no_match_name_only(self):
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="todoist", package="todoist-mcp-server", description="", source="registry")
        assert is_in_store(r, store) is False

    def test_no_match_github_fork(self):
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="todoist", package="koki-develop/todoist-mcp-server", description="", source="github_mcp")
        assert is_in_store(r, store) is False

    def test_no_match_when_nothing_matches(self):
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="slack", package="@anthropic/mcp-server-slack", description="", source="registry")
        assert is_in_store(r, store) is False

    def test_match_by_args(self):
        store = {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}}
        r = MCPServerResult(
            name="filesystem", package="@modelcontextprotocol/server-filesystem",
            description="", source="registry",
        )
        assert is_in_store(r, store) is True

    def test_match_by_url(self):
        store = {"todoist-remote": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(
            name="todoist-local", package="ai.todoist.net/mcp",
            description="", source="registry",
        )
        assert is_in_store(r, store) is True

    def test_no_match_different_config(self):
        store = {"motherduck": {"url": "https://api.motherduck.com/mcp"}}
        r = MCPServerResult(
            name="gateway", package="centralmind/gateway",
            description="Universal MCP-Server", source="github_mcp",
        )
        assert is_in_store(r, store) is False

    def test_no_false_positive_on_short_name(self):
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="todoist", package="some-other/todoist-mcp", description="", source="github_mcp")
        assert is_in_store(r, store) is False

    def test_match_by_command(self):
        store = {"github": {"command": "github-mcp-server", "args": ["stdio"]}}
        r = MCPServerResult(name="github", package="github-mcp-server", description="", source="registry")
        assert is_in_store(r, store) is True

    def test_empty_store(self):
        r = MCPServerResult(name="test", package="test-pkg", description="", source="registry")
        assert is_in_store(r, store={}) is False
