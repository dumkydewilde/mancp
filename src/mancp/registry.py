"""Search MCP server registries and discover popular servers."""

import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = Path.home() / ".config" / "mancp" / "cache"
CACHE_TTL = 86400  # 24 hours


_cached_github_headers: dict[str, str] | None = None


def _github_headers() -> dict[str, str]:
    """Build GitHub API headers, including auth token if available.

    Caches the result so `gh auth token` is only called once per process.
    """
    global _cached_github_headers
    if _cached_github_headers is not None:
        return _cached_github_headers

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "mancp",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                token = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if token:
        headers["Authorization"] = f"token {token}"
    _cached_github_headers = headers
    return headers


@dataclass
class MCPServerResult:
    """A search result from a registry."""

    name: str
    package: str
    description: str
    source: str  # "github_mcp", "registry"
    url: str = ""
    stars: int = 0
    keywords: list[str] = field(default_factory=list)
    author: str = ""
    license: str = ""
    version: str = ""
    install_hint: str = ""
    transport: str = ""  # "stdio", "streamable-http", etc.
    registry_name: str = ""  # full registry name e.g. "net.todoist/mcp"
    remote_url: str = ""  # for streamable-http servers

    def to_mcp_config(self) -> dict:
        """Generate a Claude Code MCP server config."""
        if self.remote_url:
            return {"url": self.remote_url}
        return {
            "command": "npx",
            "args": ["-y", self.package],
        }

    def display_line(self) -> str:
        stars = f"  [{self.stars}*]" if self.stars else ""
        return f"{self.name:<30} {self.description[:50]}{stars}"


def search_github_mcp_org(query: str, size: int = 15) -> list[MCPServerResult]:
    """Search GitHub for repos in the modelcontextprotocol org matching query."""
    params = urllib.parse.urlencode({
        "q": f"org:modelcontextprotocol {query} in:name,description",
        "sort": "stars",
        "order": "desc",
        "per_page": size,
    })
    url = f"https://api.github.com/search/repositories?{params}"
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []

    results = []
    for repo in data.get("items", []):
        name = repo.get("name", "")
        full_name = repo.get("full_name", "")
        desc = repo.get("description", "") or ""

        short = name
        if short.startswith("server-"):
            short = short[len("server-"):]
        elif short.startswith("mcp-server-"):
            short = short[len("mcp-server-"):]
        elif short.endswith("-mcp-server"):
            short = short[:-len("-mcp-server")]

        results.append(MCPServerResult(
            name=short,
            package=full_name,
            description=desc[:100],
            source="github_mcp",
            url=repo.get("html_url", ""),
            stars=repo.get("stargazers_count", 0),
            keywords=repo.get("topics", []),
        ))
    return results


def search_mcp_registry(query: str, size: int = 15) -> list[MCPServerResult]:
    """Search the official MCP registry at registry.modelcontextprotocol.io."""
    params = urllib.parse.urlencode({
        "search": query,
        "limit": size,
        "version": "latest",
    })
    url = f"https://registry.modelcontextprotocol.io/v0.1/servers?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "mancp",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []

    results = []
    for entry in data.get("servers", []):
        server = entry.get("server", {})
        meta = entry.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})

        full_name = server.get("name", "")
        desc = server.get("description", "") or ""
        version = server.get("version", "")
        repo = server.get("repository", {})
        repo_url = repo.get("url", "")

        # Derive short name from the registry name (e.g. "io.github.user/mcp-server-foo")
        short = full_name.split("/")[-1] if "/" in full_name else full_name
        for prefix in ("mcp-server-", "server-", "mcp-"):
            if short.startswith(prefix):
                short = short[len(prefix):]
                break
        for suffix in ("-mcp-server", "-mcp"):
            if short.endswith(suffix):
                short = short[:-len(suffix)]
                break

        # Get transport type, package identifier, and remote URL
        packages = server.get("packages", [])
        remotes = server.get("remotes", [])
        pkg_id = ""
        install_hint = ""
        transport = ""
        remote_url = ""

        # Check remotes first (streamable-http, sse, etc.)
        if remotes:
            remote = remotes[0]
            transport = remote.get("type", "")
            remote_url = remote.get("url", "")

        # Then check packages (npm, pypi, etc.)
        for pkg in packages:
            reg_type = pkg.get("registryType", "")
            identifier = pkg.get("identifier", "")
            pkg_transport = pkg.get("transport", {}).get("type", "")
            if not transport and pkg_transport:
                transport = pkg_transport
            if reg_type == "npm":
                pkg_id = identifier
                install_hint = f"npx -y {identifier}"
                break
            elif reg_type == "pypi":
                pkg_id = identifier
                install_hint = f"uvx {identifier}"
            elif not pkg_id:
                pkg_id = identifier

        if not pkg_id and not remote_url:
            pkg_id = full_name

        results.append(MCPServerResult(
            name=short,
            package=pkg_id,
            description=desc[:100],
            source="registry",
            url=repo_url,
            version=version,
            install_hint=install_hint,
            transport=transport,
            registry_name=full_name,
            remote_url=remote_url,
        ))
    return results


def is_in_store(result: "MCPServerResult", store: dict) -> bool:
    """Check if a search result matches any entry in the store."""
    pkg_lower = result.package.lower()
    for name, cfg in store.items():
        args = cfg.get("args", [])
        for arg in args:
            if isinstance(arg, str) and arg.lower() == pkg_lower:
                return True
        url = cfg.get("url", "")
        if url and pkg_lower in url.lower():
            return True
        cmd = cfg.get("command", "")
        if cmd and cmd.lower() == pkg_lower:
            return True
    return False


def search_all(query: str, size: int = 10) -> list[MCPServerResult]:
    """Search GitHub MCP org and MCP registry, deduplicate, return merged results.

    GitHub MCP org results shown first, then registry results.
    """
    gh_results = search_github_mcp_org(query, size=size)
    registry_results = search_mcp_registry(query, size=size)

    seen_short: set[str] = set()
    github_merged: list[MCPServerResult] = []
    registry_merged: list[MCPServerResult] = []

    for r in gh_results:
        short = r.name.lower()
        seen_short.add(short)
        github_merged.append(r)

    for r in registry_results:
        short = r.name.lower()
        if short not in seen_short:
            seen_short.add(short)
            registry_merged.append(r)

    return github_merged + registry_merged


# -- Discover: categories + repos from awesome-mcp-servers --

AWESOME_README_URL = (
    "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md"
)

# Minimum entries to show a category
MIN_CATEGORY_ENTRIES = 5


@dataclass
class DiscoverServer:
    """A server entry for the discover tab."""
    name: str
    author: str
    description: str
    url: str
    license: str = ""
    category: str = ""
    stars: int = 0
    forks: int = 0
    language: str = ""


@dataclass
class CategoryEntry:
    """A raw entry parsed from the awesome-mcp-servers README."""
    owner: str
    repo: str
    url: str
    description: str
    badges: str = ""


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def _cache_is_fresh(name: str) -> bool:
    p = _cache_path(name)
    if not p.exists():
        return False
    return (time.time() - p.stat().st_mtime) < CACHE_TTL


def _cache_read(name: str) -> list | dict | None:
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _cache_write(name: str, data: list | dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(name).write_text(json.dumps(data))


def _parse_awesome_readme(text: str) -> dict[str, list[CategoryEntry]]:
    """Parse the awesome-mcp-servers README into categories with repo entries."""
    categories: dict[str, list[CategoryEntry]] = {}
    current_cat = None

    for line in text.splitlines():
        # Match category headers: ### emoji <a name="..."></a>Category Name
        m = re.match(r'^### .+?</a>\s*(.+)', line)
        if m:
            current_cat = m.group(1).strip()
            categories[current_cat] = []
            continue

        if current_cat is None:
            continue

        # Match entries: - [owner/repo](https://github.com/owner/repo) badges - description
        m = re.match(
            r'^- \[([^/\]]+)/([^\]]+)\]\((https://github\.com/[^\)]+)\)\s*(.*)',
            line,
        )
        if m:
            owner, repo, url, rest = m.groups()
            # Split rest into badges and description at the " - " separator
            parts = rest.split(" - ", 1)
            if len(parts) == 2:
                badges, desc = parts
            else:
                badges, desc = "", rest
            categories[current_cat].append(CategoryEntry(
                owner=owner.strip(),
                repo=repo.strip(),
                url=url.strip(),
                description=desc.strip()[:120],
                badges=badges.strip(),
            ))

    return categories


def fetch_awesome_categories() -> list[tuple[str, int]]:
    """Fetch and parse categories from awesome-mcp-servers README.

    Returns list of (category_name, entry_count) sorted by count desc.
    Cached for 24 hours.
    """
    cache_name = "awesome_categories"
    if _cache_is_fresh(cache_name):
        cached = _cache_read(cache_name)
        if cached and isinstance(cached, list):
            return [(c["name"], c["count"]) for c in cached]

    text = _fetch_awesome_readme()
    if not text:
        # Fallback to hardcoded if fetch fails
        return _FALLBACK_CATEGORIES

    categories = _parse_awesome_readme(text)
    result = [
        (name, len(entries))
        for name, entries in categories.items()
        if len(entries) >= MIN_CATEGORY_ENTRIES
    ]
    result.sort(key=lambda x: -x[1])

    _cache_write(cache_name, [{"name": n, "count": c} for n, c in result])
    return result


def _fetch_awesome_readme() -> str:
    """Fetch the awesome-mcp-servers README.md content."""
    req = urllib.request.Request(AWESOME_README_URL, headers={
        "User-Agent": "mancp",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError):
        return ""


def _fetch_github_repo(owner: str, repo: str) -> dict | None:
    """Fetch a single GitHub repo's details via the API."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None


def _entry_to_server(entry: CategoryEntry, gh_data: dict | None, category: str) -> DiscoverServer:
    """Convert a CategoryEntry + optional GitHub API data into a DiscoverServer."""
    if gh_data:
        desc = (gh_data.get("description") or entry.description or "")[:120]
        lic = gh_data.get("license") or {}
        return DiscoverServer(
            name=gh_data.get("name", entry.repo),
            author=entry.owner,
            description=desc,
            url=gh_data.get("html_url", entry.url),
            license=lic.get("spdx_id", ""),
            category=category,
            stars=gh_data.get("stargazers_count", 0),
            forks=gh_data.get("forks_count", 0),
            language=gh_data.get("language", "") or "",
        )
    return DiscoverServer(
        name=entry.repo,
        author=entry.owner,
        description=entry.description,
        url=entry.url,
        category=category,
    )


def fetch_category_servers(category: str, offset: int = 0, limit: int = 15) -> list[DiscoverServer]:
    """Fetch servers for a category from awesome-mcp-servers + GitHub API.

    Parses the README for repo URLs in the category, then fetches GitHub API
    details for `limit` repos in parallel (starting at `offset`).
    Results for each page are cached for 24 hours.
    """
    slug = category.lower().replace(" & ", "-").replace(" ", "-").replace(",", "")
    cache_name = f"awesome_cat_{slug}_p{offset}"
    if _cache_is_fresh(cache_name):
        cached = _cache_read(cache_name)
        if cached and isinstance(cached, list):
            return [DiscoverServer(**s) for s in cached]

    # Get entries from README
    entries = _get_category_entries(category)
    if not entries:
        return []

    page = entries[offset:offset + limit]
    if not page:
        return []

    # Fetch GitHub details in parallel
    servers: list[DiscoverServer] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(_fetch_github_repo, e.owner, e.repo): e
            for e in page
        }
        for future in as_completed(futures):
            entry = futures[future]
            gh_data = future.result()
            servers.append(_entry_to_server(entry, gh_data, category))

    # Sort by stars descending
    servers.sort(key=lambda s: -s.stars)

    # Only cache if most entries got GitHub data (stars > 0 means API succeeded)
    enriched = sum(1 for s in servers if s.stars > 0)
    if enriched > len(servers) // 2:
        _cache_write(cache_name, [
            {"name": s.name, "author": s.author, "description": s.description,
             "url": s.url, "license": s.license, "category": s.category,
             "stars": s.stars, "forks": s.forks, "language": s.language}
            for s in servers
        ])
    return servers


def get_category_total(category: str) -> int:
    """Get total number of entries for a category."""
    return len(_get_category_entries(category))


def _get_category_entries(category: str) -> list[CategoryEntry]:
    """Get parsed entries for a category, using cached README."""
    cache_name = "awesome_readme_parsed"
    if _cache_is_fresh(cache_name):
        cached = _cache_read(cache_name)
        if cached and isinstance(cached, dict):
            entries_data = cached.get(category, [])
            return [CategoryEntry(**e) for e in entries_data]

    text = _fetch_awesome_readme()
    if not text:
        return []

    categories = _parse_awesome_readme(text)

    # Cache all parsed entries
    cache_data = {
        cat: [{"owner": e.owner, "repo": e.repo, "url": e.url,
               "description": e.description, "badges": e.badges}
              for e in entries]
        for cat, entries in categories.items()
    }
    _cache_write(cache_name, cache_data)

    return categories.get(category, [])


def fetch_popular_servers() -> list[DiscoverServer]:
    """Fetch popular servers by getting top-starred repos across categories.

    Picks repos from the largest categories and fetches GitHub details.
    Cached for 24 hours.
    """
    cache_name = "awesome_popular"
    if _cache_is_fresh(cache_name):
        cached = _cache_read(cache_name)
        if cached and isinstance(cached, list):
            return [DiscoverServer(**s) for s in cached]

    # Collect a sample of repos from top categories
    text = _fetch_awesome_readme()
    if not text:
        return []

    categories = _parse_awesome_readme(text)
    # Pick first few entries from each large category
    sample_entries: list[tuple[str, CategoryEntry]] = []
    sorted_cats = sorted(categories.items(), key=lambda x: -len(x[1]))
    for cat_name, entries in sorted_cats[:8]:
        for entry in entries[:5]:
            sample_entries.append((cat_name, entry))

    if not sample_entries:
        return []

    # Fetch GitHub details in parallel
    servers: list[DiscoverServer] = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {
            pool.submit(_fetch_github_repo, e.owner, e.repo): (cat, e)
            for cat, e in sample_entries
        }
        for future in as_completed(futures):
            cat, entry = futures[future]
            gh_data = future.result()
            servers.append(_entry_to_server(entry, gh_data, cat))

    # Sort by stars and take top entries
    servers.sort(key=lambda s: -s.stars)
    servers = servers[:30]

    # Only cache if most entries got GitHub data
    enriched = sum(1 for s in servers if s.stars > 0)
    if enriched > len(servers) // 2:
        _cache_write(cache_name, [
            {"name": s.name, "author": s.author, "description": s.description,
             "url": s.url, "license": s.license, "category": s.category,
             "stars": s.stars, "forks": s.forks, "language": s.language}
            for s in servers
        ])
    return servers


# Keep for backwards compat with old cache files
def fetch_categories() -> list[tuple[str, int]]:
    """Alias for fetch_awesome_categories."""
    return fetch_awesome_categories()


# -- Skills: skills.sh integration --

@dataclass
class Skill:
    """A skill entry from skills.sh."""
    name: str
    source: str  # "author/repo"
    installs: int = 0


def _fetch_skills_sh(path: str = "/") -> list[Skill]:
    """Fetch skills from skills.sh using Next.js RSC flight data."""
    cache_name = f"skills_sh_{path.strip('/') or 'alltime'}"
    if _cache_is_fresh(cache_name):
        cached = _cache_read(cache_name)
        if cached and isinstance(cached, list):
            return [Skill(**s) for s in cached]

    url = f"https://skills.sh{path}"
    req = urllib.request.Request(url, headers={
        "RSC": "1",
        "User-Agent": "mancp",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError):
        return []

    # Extract JSON arrays of skill objects from RSC flight data
    skills: list[Skill] = []
    for m in re.finditer(r'\[(\{"source":"[^]]+)\]', text):
        try:
            data = json.loads('[' + m.group(1) + ']')
            for entry in data:
                skills.append(Skill(
                    name=entry.get("name", entry.get("skillId", "")),
                    source=entry.get("source", ""),
                    installs=entry.get("installs", 0),
                ))
            break  # first match is the full list
        except (json.JSONDecodeError, KeyError):
            continue

    _cache_write(cache_name, [
        {"name": s.name, "source": s.source, "installs": s.installs}
        for s in skills
    ])
    return skills


def fetch_skills_alltime() -> list[Skill]:
    """Fetch all-time top skills from skills.sh."""
    return _fetch_skills_sh("/")


def fetch_skills_trending() -> list[Skill]:
    """Fetch 24h trending skills from skills.sh."""
    return _fetch_skills_sh("/trending")


def fetch_skills_discover(count: int = 25) -> list[Skill]:
    """Fetch a mix of trending and all-time skills from diverse authors.

    Returns up to `count` skills, picking from diverse authors.
    Merges all-time and 24h trending, deduplicates, and diversifies by author.
    """
    alltime = fetch_skills_alltime()
    trending = fetch_skills_trending()

    # Merge, trending first, then all-time
    seen: set[str] = set()
    merged: list[Skill] = []
    for s in trending + alltime:
        key = f"{s.source}/{s.name}"
        if key not in seen:
            seen.add(key)
            merged.append(s)

    # Pick diverse authors - at most 2 per author initially, then allow more
    by_author: dict[str, list[Skill]] = {}
    for s in merged:
        author = s.source.split("/")[0] if "/" in s.source else s.source
        by_author.setdefault(author, []).append(s)

    result: list[Skill] = []
    # Round-robin across authors sorted by their best skill's installs
    sorted_authors = sorted(by_author.keys(), key=lambda a: -by_author[a][0].installs)
    max_per_author = max(2, (count // len(sorted_authors)) + 1) if sorted_authors else 2
    for round_num in range(max_per_author):
        for author in sorted_authors:
            entries = by_author[author]
            if round_num < len(entries) and len(result) < count:
                result.append(entries[round_num])

    return result[:count]



def fetch_skill_description(source: str, skill_name: str) -> str:
    """Fetch the SKILL.md description for a specific skill from GitHub.

    source is "author/repo" format, skill_name is the skill folder name.
    Searches the repo tree for <skill_name>/SKILL.md and extracts the
    front-matter description. Falls back to SKILL.md at repo root.
    Returns the description string, or empty string on failure.
    """
    import base64

    # Find the SKILL.md path via the repo tree (handles varying directory structures)
    skill_md_path = _find_skill_md_path(source, skill_name)
    if not skill_md_path:
        return ""

    url = f"https://api.github.com/repos/{source}/contents/{skill_md_path}"
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
            return _extract_skill_frontmatter_description(content)
    except Exception:
        return ""


def _find_skill_md_path(source: str, skill_name: str) -> str:
    """Find the path to a skill's SKILL.md in a repo using the git tree API.

    Looks for <skill_name>/SKILL.md anywhere in the tree,
    falling back to a root SKILL.md.
    """
    url = f"https://api.github.com/repos/{source}/git/trees/main?recursive=1"
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return ""

    target = f"{skill_name}/SKILL.md"
    root_skill_md = ""
    for item in data.get("tree", []):
        path = item.get("path", "")
        if path.endswith(target):
            return path
        if path == "SKILL.md":
            root_skill_md = path

    return root_skill_md


def _extract_skill_frontmatter_description(text: str) -> str:
    """Extract the description field from SKILL.md YAML front-matter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            # Handle both `description: text` and `description: "text"`
            val = line.split(":", 1)[1].strip()
            if val and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            return val
    return ""


def search_skills(query: str) -> list[Skill]:
    """Search skills by filtering the all-time list by query."""
    query_lower = query.lower()
    alltime = fetch_skills_alltime()
    return [s for s in alltime if query_lower in s.name.lower() or query_lower in s.source.lower()]


def get_installed_skills() -> list[dict]:
    """Get locally installed skills from ~/.claude/skills/.

    Returns list of dicts with 'name', 'description', 'path'.
    """
    skills_dir = Path.home() / ".claude" / "skills"
    if not skills_dir.exists():
        return []

    installed: list[dict] = []
    for entry in sorted(skills_dir.iterdir()):
        skill_md = entry / "SKILL.md" if entry.is_dir() else None
        # Handle symlinks
        if entry.is_symlink():
            target = entry.resolve()
            skill_md = target / "SKILL.md" if target.is_dir() else None
        if skill_md and skill_md.exists():
            desc = ""
            try:
                text = skill_md.read_text()
                desc = _extract_skill_frontmatter_description(text)
            except OSError:
                pass
            installed.append({
                "name": entry.name,
                "description": desc,
                "path": str(entry),
            })
    return installed


# Fallback categories if README fetch fails
_FALLBACK_CATEGORIES = [
    ("Developer Tools", 219),
    ("Finance & Fintech", 159),
    ("Other Tools and Integrations", 148),
    ("Search & Data Extraction", 109),
    ("Databases", 98),
    ("Knowledge & Memory", 79),
    ("Security", 66),
    ("Communication", 55),
    ("Cloud Platforms", 52),
    ("Aggregators", 45),
    ("Coding Agents", 45),
    ("Browser Automation", 38),
    ("Art & Culture", 31),
    ("Workplace & Productivity", 29),
    ("Monitoring", 27),
    ("Data Science Tools", 23),
    ("Location Services", 22),
    ("File Systems", 20),
    ("Data Platforms", 16),
    ("Gaming", 16),
    ("Marketing", 16),
    ("Version Control", 14),
    ("Multimedia Process", 14),
]
