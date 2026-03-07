"""Tests for mancp.registry module."""

import json
from unittest.mock import patch, MagicMock

from mancp.registry import (
    MCPServerResult,
    is_in_store,
    search_npm,
    search_github,
    search_all,
)


def _fake_npm_response(packages: list[dict]) -> bytes:
    """Build a fake npm search API response."""
    return json.dumps({
        "objects": [
            {"package": pkg} for pkg in packages
        ],
    }).encode()


def _fake_github_response(repos: list[dict]) -> bytes:
    """Build a fake GitHub search API response."""
    return json.dumps({
        "total_count": len(repos),
        "items": repos,
    }).encode()


class TestMCPServerResult:
    def test_to_mcp_config_npm(self):
        r = MCPServerResult(
            name="filesystem",
            package="@modelcontextprotocol/server-filesystem",
            description="File system MCP",
            source="npm",
        )
        cfg = r.to_mcp_config()
        assert cfg == {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        }

    def test_display_line(self):
        r = MCPServerResult(
            name="test-server",
            package="test-pkg",
            description="A test server",
            source="npm",
            stars=42,
        )
        line = r.display_line()
        assert "test-server" in line
        assert "42*" in line


class TestSearchNpm:
    def test_parses_results(self):
        fake_data = _fake_npm_response([
            {
                "name": "@modelcontextprotocol/server-filesystem",
                "description": "MCP server for filesystem access",
                "keywords": ["mcp", "mcp-server"],
                "links": {"npm": "https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem"},
            },
        ])
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mancp.registry.urllib.request.urlopen", return_value=mock_resp):
            results = search_npm("filesystem")

        assert len(results) == 1
        assert results[0].name == "filesystem"
        assert results[0].source == "npm"

    def test_filters_non_mcp(self):
        fake_data = _fake_npm_response([
            {
                "name": "unrelated-package",
                "description": "A JavaScript utility library",
                "keywords": ["javascript"],
                "links": {},
            },
        ])
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mancp.registry.urllib.request.urlopen", return_value=mock_resp):
            results = search_npm("something")

        assert len(results) == 0

    def test_handles_network_error(self):
        with patch("mancp.registry.urllib.request.urlopen", side_effect=TimeoutError):
            results = search_npm("test")
        assert results == []

    def test_strips_common_prefixes(self):
        fake_data = _fake_npm_response([
            {
                "name": "@anthropic/mcp-server-slack",
                "description": "Slack MCP",
                "keywords": ["mcp"],
                "links": {},
            },
            {
                "name": "some-mcp-server-postgres",
                "description": "Postgres MCP",
                "keywords": ["mcp"],
                "links": {},
            },
        ])
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mancp.registry.urllib.request.urlopen", return_value=mock_resp):
            results = search_npm("test")

        assert results[0].name == "slack"
        assert results[1].name == "some-mcp-server-postgres"


class TestSearchGithub:
    def test_parses_results(self):
        fake_data = _fake_github_response([
            {
                "name": "mcp-server-sqlite",
                "full_name": "user/mcp-server-sqlite",
                "description": "SQLite MCP server",
                "topics": ["mcp", "mcp-server"],
                "html_url": "https://github.com/user/mcp-server-sqlite",
                "stargazers_count": 150,
            },
        ])
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mancp.registry.urllib.request.urlopen", return_value=mock_resp):
            results = search_github("sqlite")

        assert len(results) == 1
        assert results[0].name == "sqlite"
        assert results[0].stars == 150
        assert results[0].source == "github"

    def test_handles_network_error(self):
        with patch("mancp.registry.urllib.request.urlopen", side_effect=TimeoutError):
            results = search_github("test")
        assert results == []


class TestSearchAll:
    def test_deduplicates(self):
        npm_result = MCPServerResult(
            name="filesystem", package="@mcp/server-filesystem",
            description="FS", source="npm",
        )
        gh_result = MCPServerResult(
            name="filesystem", package="@mcp/server-filesystem",
            description="FS", source="github",
        )
        with (
            patch("mancp.registry.search_npm", return_value=[npm_result]),
            patch("mancp.registry.search_github", return_value=[gh_result]),
        ):
            results = search_all("filesystem")

        assert len(results) == 1
        assert results[0].source == "github"  # github takes priority (vetted)

    def test_merges_unique(self):
        npm_result = MCPServerResult(
            name="fs", package="@mcp/server-fs",
            description="FS", source="npm",
        )
        gh_result = MCPServerResult(
            name="sqlite", package="user/mcp-server-sqlite",
            description="SQLite", source="github",
        )
        with (
            patch("mancp.registry.search_npm", return_value=[npm_result]),
            patch("mancp.registry.search_github", return_value=[gh_result]),
        ):
            results = search_all("test")

        assert len(results) == 2


class TestIsInStore:
    def test_no_match_name_only(self):
        """Name-only match is not enough — different packages can share a short name."""
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="todoist", package="todoist-mcp-server", description="", source="npm")
        assert is_in_store(r, store) is False

    def test_no_match_github_fork(self):
        """A GitHub fork with same short name should NOT match a URL-based entry."""
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="todoist", package="koki-develop/todoist-mcp-server", description="", source="github")
        assert is_in_store(r, store) is False

    def test_no_match_when_nothing_matches(self):
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="slack", package="@anthropic/mcp-server-slack", description="", source="npm")
        assert is_in_store(r, store) is False

    def test_match_by_args(self):
        store = {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}}
        r = MCPServerResult(
            name="filesystem", package="@modelcontextprotocol/server-filesystem",
            description="", source="npm",
        )
        assert is_in_store(r, store) is True

    def test_match_by_url(self):
        """Package string found inside a store entry's URL."""
        store = {"todoist-remote": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(
            name="todoist-local", package="ai.todoist.net/mcp",
            description="", source="npm",
        )
        assert is_in_store(r, store) is True

    def test_no_match_different_config(self):
        """A GitHub repo shouldn't match an unrelated store entry."""
        store = {"motherduck": {"url": "https://api.motherduck.com/mcp"}}
        r = MCPServerResult(
            name="gateway", package="centralmind/gateway",
            description="Universal MCP-Server", source="github",
        )
        assert is_in_store(r, store) is False

    def test_no_false_positive_on_short_name(self):
        """Different packages that strip to the same short name shouldn't match."""
        store = {"todoist": {"url": "https://ai.todoist.net/mcp"}}
        r = MCPServerResult(name="todoist", package="some-other/todoist-mcp", description="", source="github")
        assert is_in_store(r, store) is False

    def test_match_by_command(self):
        store = {"github": {"command": "github-mcp-server", "args": ["stdio"]}}
        r = MCPServerResult(name="github", package="github-mcp-server", description="", source="npm")
        assert is_in_store(r, store) is True

    def test_empty_store(self):
        r = MCPServerResult(name="test", package="test-pkg", description="", source="npm")
        assert is_in_store(r, store={}) is False
