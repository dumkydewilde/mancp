"""Search MCP server registries (npm, GitHub)."""

import json
import urllib.request
import urllib.parse
from dataclasses import dataclass, field


@dataclass
class MCPServerResult:
    """A search result from a registry."""

    name: str
    package: str
    description: str
    source: str  # "npm" or "github"
    url: str = ""
    stars: int = 0
    keywords: list[str] = field(default_factory=list)

    def to_mcp_config(self) -> dict:
        """Generate a Claude Code MCP server config."""
        if self.source == "npm":
            return {
                "command": "npx",
                "args": ["-y", self.package],
            }
        # GitHub repos — use npx if it looks like an npm package, else uvx/docker
        return {
            "command": "npx",
            "args": ["-y", self.package],
        }

    def display_line(self) -> str:
        stars = f"  [{self.stars}*]" if self.stars else ""
        return f"{self.name:<30} {self.description[:50]}{stars}"


def search_npm(query: str, size: int = 20) -> list[MCPServerResult]:
    """Search npm registry for MCP server packages."""
    params = urllib.parse.urlencode({
        "text": f"mcp server {query}",
        "size": size,
    })
    url = f"https://registry.npmjs.org/-/v1/search?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []

    results = []
    for obj in data.get("objects", []):
        pkg = obj.get("package", {})
        name = pkg.get("name", "")
        # Filter to likely MCP servers
        keywords = [k.lower() for k in pkg.get("keywords", [])]
        name_lower = name.lower()
        desc_lower = pkg.get("description", "").lower()
        is_mcp_server = (
            "mcp-server" in name_lower
            or "mcp_server" in name_lower
            or "mcp server" in desc_lower
            or "mcp-server" in keywords
            or ("mcp" in keywords and "server" in desc_lower)
        )
        if not is_mcp_server:
            continue
        # Derive a short name for the store
        short = name
        for prefix in ("@modelcontextprotocol/server-", "@anthropic/mcp-server-", "@anthropic-ai/mcp-server-"):
            if name.startswith(prefix):
                short = name[len(prefix):]
                break
        if short.startswith("mcp-server-"):
            short = short[len("mcp-server-"):]
        elif short.endswith("-mcp-server"):
            short = short[:-len("-mcp-server")]
        elif short.endswith("-mcp"):
            short = short[:-len("-mcp")]

        results.append(MCPServerResult(
            name=short,
            package=name,
            description=pkg.get("description", ""),
            source="npm",
            url=pkg.get("links", {}).get("npm", ""),
            keywords=pkg.get("keywords", []),
        ))
    return results


def search_github(query: str, size: int = 20) -> list[MCPServerResult]:
    """Search GitHub for MCP server repositories."""
    params = urllib.parse.urlencode({
        "q": f"{query} mcp server in:name,description,topics",
        "sort": "stars",
        "order": "desc",
        "per_page": size,
    })
    url = f"https://api.github.com/search/repositories?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "mancp",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []

    results = []
    for repo in data.get("items", []):
        name = repo.get("name", "")
        full_name = repo.get("full_name", "")
        topics = repo.get("topics", [])
        desc = repo.get("description", "") or ""

        # Derive short name
        short = name
        if short.startswith("mcp-server-"):
            short = short[len("mcp-server-"):]
        elif short.endswith("-mcp-server"):
            short = short[:-len("-mcp-server")]
        elif short.endswith("-mcp"):
            short = short[:-len("-mcp")]

        results.append(MCPServerResult(
            name=short,
            package=full_name,
            description=desc[:100],
            source="github",
            url=repo.get("html_url", ""),
            stars=repo.get("stargazers_count", 0),
            keywords=topics,
        ))
    return results


def is_in_store(result: "MCPServerResult", store: dict) -> bool:
    """Check if a search result matches any entry in the store.

    Compares the package identifier against command args, URLs, and commands
    in existing store configs. A name-only match is not sufficient since
    multiple different packages can strip to the same short name.
    """
    pkg_lower = result.package.lower()
    for name, cfg in store.items():
        # Match against npm package in args
        args = cfg.get("args", [])
        for arg in args:
            if isinstance(arg, str) and arg.lower() == pkg_lower:
                return True
        # Match against URL
        url = cfg.get("url", "")
        if url and pkg_lower in url.lower():
            return True
        # Match against command
        cmd = cfg.get("command", "")
        if cmd and cmd.lower() == pkg_lower:
            return True
    return False


def search_all(query: str, size: int = 10) -> list[MCPServerResult]:
    """Search both npm and GitHub, deduplicate, return merged results.

    GitHub results are shown first (vetted repos with stars) followed by
    npm-only results.  Deduplication matches on both exact package name
    and short name so an npm package that wraps a GitHub repo is collapsed.
    """
    npm_results = search_npm(query, size=size)
    gh_results = search_github(query, size=size)

    seen_packages: set[str] = set()
    seen_short: set[str] = set()
    github_merged: list[MCPServerResult] = []
    npm_merged: list[MCPServerResult] = []

    # GitHub results first (vetted, have stars)
    for r in gh_results:
        key = r.package.lower()
        short = r.name.lower()
        seen_packages.add(key)
        seen_short.add(short)
        github_merged.append(r)

    # npm results second, dedup against GitHub by package AND short name
    for r in npm_results:
        key = r.package.lower()
        short = r.name.lower()
        if key not in seen_packages and short not in seen_short:
            seen_packages.add(key)
            seen_short.add(short)
            npm_merged.append(r)

    return github_merged + npm_merged
