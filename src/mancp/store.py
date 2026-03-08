"""File I/O and MCP store management."""

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "mancp"
STORE_FILE = CONFIG_DIR / "mcps.json"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_DIR = Path.home() / ".claude"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"

import platform

def _claude_desktop_dir() -> Path:
    """Return the Claude Desktop config directory (macOS only for now)."""
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    # Windows: Path(os.environ.get("APPDATA", "")) / "Claude"
    # Linux: ~/.config/Claude
    return Path.home() / ".config" / "Claude"

CLAUDE_DESKTOP_DIR = _claude_desktop_dir()
CLAUDE_DESKTOP_CONFIG = CLAUDE_DESKTOP_DIR / "claude_desktop_config.json"
CLAUDE_DESKTOP_EXTENSIONS = CLAUDE_DESKTOP_DIR / "Claude Extensions"


def load_store(store_file: Path = STORE_FILE) -> dict:
    if not store_file.exists():
        return {}
    return json.loads(store_file.read_text())


def save_store(store: dict, store_file: Path = STORE_FILE) -> None:
    store_file.parent.mkdir(parents=True, exist_ok=True)
    store_file.write_text(json.dumps(store, indent=2))



def load_claude_json(claude_json: Path = CLAUDE_JSON) -> dict:
    if not claude_json.exists():
        return {}
    return json.loads(claude_json.read_text())


def save_claude_json(data: dict, claude_json: Path = CLAUDE_JSON) -> None:
    if claude_json.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(claude_json, claude_json.with_suffix(f".json.bak.{ts}"))
    claude_json.write_text(json.dumps(data, indent=2))


def load_project_mcp(d: Path) -> dict:
    f = d / ".mcp.json"
    return json.loads(f.read_text()) if f.exists() else {"mcpServers": {}}


def save_project_mcp(d: Path, data: dict) -> Path:
    f = d / ".mcp.json"
    f.write_text(json.dumps(data, indent=2))
    return f


def get_user_scope_mcps(claude_json: Path = CLAUDE_JSON) -> set[str]:
    """Names of MCPs currently active in ~/.claude.json user scope."""
    return set(load_claude_json(claude_json).get("mcpServers", {}).keys())


def get_project_scope_mcps(cwd: Path) -> set[str]:
    """Names of MCPs currently in the project's .mcp.json."""
    return set(load_project_mcp(cwd).get("mcpServers", {}).keys())


def collect_all_from_claude_json(claude_json: Path = CLAUDE_JSON) -> dict:
    """All MCPs from ~/.claude.json: global + every project scope."""
    data = load_claude_json(claude_json)
    out = {}
    for name, cfg in data.get("mcpServers", {}).items():
        out[name] = cfg
    for pd in data.get("projects", {}).values():
        for name, cfg in pd.get("mcpServers", {}).items():
            if name not in out:
                out[name] = cfg
    return out


def collect_readonly_mcps(claude_dir: Path = CLAUDE_DIR) -> dict[str, str]:
    """Collect read-only MCPs: claude.ai connectors, plugins, ~/.claude/.mcp.json.

    Returns dict of {name: status} where status is a display string.
    """
    readonly: dict[str, str] = {}

    # claude.ai OAuth connectors
    # mcp-needs-auth-cache.json has MCPs that need authentication
    needs_auth: set[str] = set()
    auth_cache = claude_dir / "mcp-needs-auth-cache.json"
    if auth_cache.exists():
        try:
            data = json.loads(auth_cache.read_text())
            for name in data:
                needs_auth.add(name)
                readonly[name] = "needs auth"
        except (json.JSONDecodeError, OSError):
            pass

    # settings.local.json permissions reveal connected cloud MCPs
    settings_local = claude_dir / "settings.local.json"
    if settings_local.exists():
        try:
            data = json.loads(settings_local.read_text())
            for perm in data.get("permissions", {}).get("allow", []):
                m = re.match(r"mcp__claude_ai_(\w+)__", perm)
                if m:
                    # Convert underscore-separated to space-separated
                    raw = m.group(1).replace("_", " ")
                    name = f"claude.ai {raw}"
                    if name not in needs_auth and name not in readonly:
                        readonly[name] = "connected"
        except (json.JSONDecodeError, OSError):
            pass

    # Plugins
    settings = claude_dir / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            for plugin_id, enabled in data.get("enabledPlugins", {}).items():
                status = "plugin" if enabled else "plugin (disabled)"
                name = plugin_id.split("@")[0]
                readonly[f"plugin:{name}"] = status
        except (json.JSONDecodeError, OSError):
            pass

    # User-level ~/.claude/.mcp.json
    user_mcp = claude_dir / ".mcp.json"
    if user_mcp.exists():
        try:
            data = json.loads(user_mcp.read_text())
            for name in data.get("mcpServers", {}):
                readonly[name] = "~/.claude/.mcp.json"
        except (json.JSONDecodeError, OSError):
            pass

    # Claude Desktop MCPs (only from default location, not test dirs)
    if claude_dir == CLAUDE_DIR:
        desktop_mcps = collect_desktop_mcps()
        for name, status in desktop_mcps.items():
            if name not in readonly:
                readonly[name] = status

    return readonly


def categorize_readonly(readonly: dict[str, str]) -> dict[str, dict[str, str]]:
    """Split readonly MCPs into categories for display.

    Returns dict of {category: {name: status}}.
    """
    cats: dict[str, dict[str, str]] = {
        "cloud": {},
        "plugin": {},
        "user_mcp": {},
        "desktop": {},
        "desktop_ext": {},
    }
    for name, status in readonly.items():
        if name.startswith("claude.ai"):
            cats["cloud"][name] = status
        elif name.startswith("plugin:"):
            cats["plugin"][name] = status
        elif status.startswith("desktop"):
            if "extension" in status:
                cats["desktop_ext"][name] = status
            else:
                cats["desktop"][name] = status
        else:
            cats["user_mcp"][name] = status
    return {k: v for k, v in cats.items() if v}


def load_desktop_config() -> dict:
    """Load Claude Desktop's claude_desktop_config.json."""
    if not CLAUDE_DESKTOP_CONFIG.exists():
        return {}
    try:
        return json.loads(CLAUDE_DESKTOP_CONFIG.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def get_desktop_mcps(config: Path = CLAUDE_DESKTOP_CONFIG) -> dict[str, dict]:
    """Get MCP server configs from Claude Desktop config.

    Returns {name: config_dict} for servers in claude_desktop_config.json.
    """
    if not config.exists():
        return {}
    try:
        data = json.loads(config.read_text())
        return data.get("mcpServers", {})
    except (json.JSONDecodeError, OSError):
        return {}


def get_desktop_extensions(ext_dir: Path = CLAUDE_DESKTOP_EXTENSIONS) -> list[dict]:
    """Get installed Claude Desktop extensions with metadata from manifest.json.

    Returns list of dicts with keys: name, display_name, description, author,
    tools, tool_count, path.
    """
    if not ext_dir.exists():
        return []
    extensions = []
    for entry in sorted(ext_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text())
            tools = data.get("tools", [])
            extensions.append({
                "name": entry.name,
                "display_name": data.get("display_name", entry.name),
                "description": data.get("description", ""),
                "author": data.get("author", {}).get("name", "") if isinstance(data.get("author"), dict) else str(data.get("author", "")),
                "tools": tools,
                "tool_count": len(tools),
                "path": str(entry),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return extensions


def collect_desktop_mcps(desktop_dir: Path = CLAUDE_DESKTOP_DIR) -> dict[str, str]:
    """Collect read-only MCPs from Claude Desktop config and extensions.

    Returns {name: status_string}.
    """
    config = desktop_dir / "claude_desktop_config.json"
    ext_dir = desktop_dir / "Claude Extensions"
    result: dict[str, str] = {}

    # Desktop MCP servers
    for name in get_desktop_mcps(config):
        result[name] = "desktop"

    # Desktop extensions
    for ext in get_desktop_extensions(ext_dir):
        result[ext["display_name"]] = "desktop extension"

    return result


TOOL_COUNTS_CACHE = CONFIG_DIR / "cache" / "tool_counts.json"
TOOL_COUNTS_TTL = 86400 * 7  # 7 days


def load_tool_counts_cache() -> dict[str, int]:
    """Load cached tool counts from disk."""
    if not TOOL_COUNTS_CACHE.exists():
        return {}
    try:
        data = json.loads(TOOL_COUNTS_CACHE.read_text())
        # Check TTL
        ts = data.get("_updated", 0)
        if time.time() - ts > TOOL_COUNTS_TTL:
            return {}
        return {k: v for k, v in data.items() if k != "_updated" and isinstance(v, int)}
    except (json.JSONDecodeError, OSError):
        return {}


def save_tool_counts_cache(counts: dict[str, int]) -> None:
    """Persist tool counts cache to disk."""
    TOOL_COUNTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    data = {**counts, "_updated": int(time.time())}
    TOOL_COUNTS_CACHE.write_text(json.dumps(data, indent=2))


def count_mcp_tools(claude_dir: Path = CLAUDE_DIR) -> dict[str, int]:
    """Count permitted tools per MCP from settings.local.json.

    Returns dict of {mcp_prefix: tool_count}.
    """
    counts: dict[str, int] = {}
    settings_local = claude_dir / "settings.local.json"
    if not settings_local.exists():
        return counts
    try:
        data = json.loads(settings_local.read_text())
        for perm in data.get("permissions", {}).get("allow", []):
            m = re.match(r"mcp__([^_]+(?:[_][^_]+)*)__", perm)
            if m:
                prefix = m.group(1)
                counts[prefix] = counts.get(prefix, 0) + 1
    except (json.JSONDecodeError, OSError):
        pass
    return counts


def get_mcp_tool_names(claude_dir: Path = CLAUDE_DIR) -> dict[str, list[str]]:
    """Get tool names per MCP from settings.local.json.

    Returns dict of {mcp_prefix: [tool_name, ...]}.
    """
    tools: dict[str, list[str]] = {}
    settings_local = claude_dir / "settings.local.json"
    if not settings_local.exists():
        return tools
    try:
        data = json.loads(settings_local.read_text())
        for perm in data.get("permissions", {}).get("allow", []):
            m = re.match(r"mcp__([^_]+(?:[_][^_]+)*)__(.+)", perm)
            if m:
                prefix = m.group(1)
                tool = m.group(2)
                tools.setdefault(prefix, []).append(tool)
    except (json.JSONDecodeError, OSError):
        pass
    return tools


def estimate_tool_count(
    name: str,
    cfg: dict,
    permission_counts: dict[str, int],
    cached_counts: dict[str, int] | None = None,
) -> int:
    """Estimate tool count for an MCP using multiple strategies.

    1. Cached tool counts from source repo analysis (best estimate of total tools)
    2. Permission-based count from settings.local.json (lower bound — only permitted tools)
    3. Return 0 if unknown
    """
    cache = cached_counts or {}
    cmd = cfg.get("command", "")
    url = cfg.get("url", "")
    args = cfg.get("args", [])

    # 1. Check cache by name
    if name in cache:
        return cache[name]

    # 2. Check cache by URL pattern
    if url:
        for pattern, count in cache.items():
            if pattern in url:
                return count

    # 3. Check cache by command basename
    cmd_base = cmd.rsplit("/", 1)[-1] if cmd else ""
    if cmd_base and cmd_base in cache:
        return cache[cmd_base]

    # 4. Check cache by args (npm package names etc.)
    for arg in args:
        if isinstance(arg, str) and arg in cache:
            return cache[arg]

    # 5. Fall back to permission counts (lower bound: only tools user has allowed)
    if name in permission_counts:
        return permission_counts[name]
    alt = name.replace("-", "_")
    if alt in permission_counts:
        return permission_counts[alt]

    return 0


def tool_count_for(name: str, tool_counts: dict[str, int]) -> int:
    """Look up tool count for an MCP by name, trying hyphen/underscore variants."""
    if name in tool_counts:
        return tool_counts[name]
    alt = name.replace("-", "_")
    if alt in tool_counts:
        return tool_counts[alt]
    return 0


# ~300 tokens per tool definition on average (name + description + JSON schema)
TOKENS_PER_TOOL = 300


def estimate_mcp_tokens(tool_count: int) -> int:
    """Estimate token cost for an MCP based on tool count."""
    return tool_count * TOKENS_PER_TOOL


def token_warning_level(tokens: int) -> str:
    """Return warning level: high / medium / low."""
    if tokens >= 5000:
        return "high"
    if tokens >= 2000:
        return "medium"
    return "low"


def load_settings(settings_json: Path = SETTINGS_JSON) -> dict:
    if not settings_json.exists():
        return {}
    return json.loads(settings_json.read_text())


def save_settings(data: dict, settings_json: Path = SETTINGS_JSON) -> None:
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    settings_json.write_text(json.dumps(data, indent=2))


def get_plugins(settings_json: Path = SETTINGS_JSON) -> dict[str, bool]:
    """Return {plugin_id: enabled} from settings.json."""
    return load_settings(settings_json).get("enabledPlugins", {})


def set_plugin_enabled(
    plugin_id: str,
    enabled: bool,
    settings_json: Path = SETTINGS_JSON,
) -> None:
    """Toggle a single plugin in settings.json."""
    data = load_settings(settings_json)
    data.setdefault("enabledPlugins", {})[plugin_id] = enabled
    save_settings(data, settings_json)


def apply_plugin_changes(
    plugins: dict[str, bool],
    settings_json: Path = SETTINGS_JSON,
) -> str:
    """Write plugin enabled states to settings.json. Returns status msg."""
    data = load_settings(settings_json)
    data["enabledPlugins"] = plugins
    save_settings(data, settings_json)
    enabled = sum(1 for v in plugins.values() if v)
    return f"Plugins: {enabled}/{len(plugins)} enabled"


def mask_secrets(cfg: dict) -> dict:
    """Return a copy with likely-secret values masked."""
    SECRET_KEYS = {
        "token", "key", "secret", "password", "authorization",
        "api_key", "apikey", "motherduck_token", "bearer",
    }

    def _mask(v: str) -> str:
        if len(v) <= 8:
            return "***"
        return v[:4] + "..." + v[-4:]

    def _scrub(obj):
        if isinstance(obj, dict):
            return {
                k: _mask(v) if isinstance(v, str) and
                any(s in k.lower() for s in SECRET_KEYS) else _scrub(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_scrub(i) for i in obj]
        return obj

    return _scrub(cfg)


def remove_mcp_everywhere(
    name: str,
    cwd: Path,
    claude_json: Path = CLAUDE_JSON,
) -> None:
    """Remove an MCP from all scopes: user, current project, and all project scopes in ~/.claude.json."""
    # Remove from ~/.claude.json global + all project scopes
    claude_data = load_claude_json(claude_json)
    claude_data.get("mcpServers", {}).pop(name, None)
    for proj_data in claude_data.get("projects", {}).values():
        proj_data.get("mcpServers", {}).pop(name, None)
    save_claude_json(claude_data, claude_json)

    # Remove from current project's .mcp.json
    mcp_file = cwd / ".mcp.json"
    if mcp_file.exists():
        proj = load_project_mcp(cwd)
        proj.get("mcpServers", {}).pop(name, None)
        save_project_mcp(cwd, proj)


PLUGINS_FILE = CLAUDE_DIR / "plugins" / "installed_plugins.json"


def load_plugin_metadata(plugin_id: str, claude_dir: Path = CLAUDE_DIR) -> dict:
    """Load metadata for a plugin from its installed cache.

    Returns dict with keys: description, author, version, homepage,
    installed_at, last_updated, mcp_config.
    """
    meta: dict = {}
    plugins_file = claude_dir / "plugins" / "installed_plugins.json"
    if not plugins_file.exists():
        return meta
    try:
        data = json.loads(plugins_file.read_text())
    except (json.JSONDecodeError, OSError):
        return meta

    installs = data.get("plugins", {}).get(plugin_id, [])
    if not installs:
        return meta

    # Use the first (active) install entry
    install = installs[0]
    install_path = Path(install.get("installPath", ""))
    meta["version"] = install.get("version", "")
    meta["installed_at"] = install.get("installedAt", "")
    meta["last_updated"] = install.get("lastUpdated", "")

    # Read plugin.json for description, author, etc.
    plugin_json = install_path / ".claude-plugin" / "plugin.json"
    if plugin_json.exists():
        try:
            pdata = json.loads(plugin_json.read_text())
            meta["description"] = pdata.get("description", "")
            author = pdata.get("author", {})
            if isinstance(author, dict):
                meta["author"] = author.get("name", "")
                if author.get("url"):
                    meta["author_url"] = author["url"]
            elif isinstance(author, str):
                meta["author"] = author
            meta["homepage"] = pdata.get("homepage", "")
            source = pdata.get("source", {})
            if isinstance(source, dict) and source.get("url"):
                meta["source_url"] = source["url"]
            meta["keywords"] = pdata.get("keywords", [])
            meta["category"] = pdata.get("category", "")
        except (json.JSONDecodeError, OSError):
            pass

    # Read .mcp.json for MCP server config
    mcp_json = install_path / ".mcp.json"
    if mcp_json.exists():
        try:
            mcp_data = json.loads(mcp_json.read_text())
            meta["mcp_config"] = mcp_data
        except (json.JSONDecodeError, OSError):
            pass

    return meta


def get_connector_server_ids(claude_dir: Path = CLAUDE_DIR) -> dict[str, str]:
    """Parse debug logs to extract cloud connector name → mcpsrv_* ID mappings.

    Returns dict of {connector_name: server_id}.
    """
    debug_dir = claude_dir / "debug"
    if not debug_dir.exists():
        return {}

    mapping: dict[str, str] = {}
    # Scan recent debug logs and accumulate all connector IDs
    logs = sorted(debug_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for log in logs[:20]:  # Check up to 20 recent logs
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        found_any = False
        for line in text.splitlines():
            # Pattern: MCP server "claude.ai X": Initializing claude.ai proxy transport for server mcpsrv_*
            m = re.match(
                r'.*MCP server "([^"]+)".*proxy transport for server (mcpsrv_\w+)',
                line,
            )
            if m:
                mapping.setdefault(m.group(1), m.group(2))
                found_any = True
        if found_any and len(mapping) >= 10:
            break  # Likely have all connectors
    return mapping


def get_org_id(claude_json: Path = CLAUDE_JSON) -> str:
    """Get the organization UUID from ~/.claude.json oauthAccount."""
    data = load_claude_json(claude_json)
    return data.get("oauthAccount", {}).get("organizationUuid", "")


def get_connector_auth_url(
    connector_name: str,
    claude_dir: Path = CLAUDE_DIR,
    claude_json: Path = CLAUDE_JSON,
) -> str | None:
    """Build the authentication URL for a cloud connector.

    Returns URL like:
    https://claude.ai/api/organizations/{org_id}/mcp/start-auth/{server_id}
    or None if data is unavailable.
    """
    org_id = get_org_id(claude_json)
    if not org_id:
        return None
    server_ids = get_connector_server_ids(claude_dir)
    server_id = server_ids.get(connector_name)
    if not server_id:
        return None
    return f"https://claude.ai/api/organizations/{org_id}/mcp/start-auth/{server_id}"


DISABLED_SKILLS_FILE = CONFIG_DIR / "disabled_skills.json"

# ~4 characters per token (rough heuristic for English text)
CHARS_PER_TOKEN = 4


def load_disabled_skills() -> set[str]:
    """Load the set of disabled skill names."""
    if not DISABLED_SKILLS_FILE.exists():
        return set()
    try:
        data = json.loads(DISABLED_SKILLS_FILE.read_text())
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def save_disabled_skills(disabled: set[str]) -> None:
    """Persist the set of disabled skill names."""
    DISABLED_SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DISABLED_SKILLS_FILE.write_text(json.dumps(sorted(disabled), indent=2))


def disable_skill(name: str, cwd: Path | None = None, scope: str = "global") -> bool:
    """Disable a skill by renaming its SKILL.md. Returns True on success."""
    if scope == "project" and cwd:
        skills_dir = cwd / ".claude" / "skills"
    else:
        skills_dir = Path.home() / ".claude" / "skills"
    entry = skills_dir / name
    if entry.is_symlink():
        target = entry.resolve()
    else:
        target = entry

    skill_md = target / "SKILL.md" if target.is_dir() else None
    if skill_md and skill_md.exists():
        skill_md.rename(target / "SKILL.md.disabled")
        disabled = load_disabled_skills()
        disabled.add(name)
        save_disabled_skills(disabled)
        return True
    return False


def enable_skill(name: str, cwd: Path | None = None, scope: str = "global") -> bool:
    """Re-enable a skill by restoring its SKILL.md. Returns True on success."""
    if scope == "project" and cwd:
        skills_dir = cwd / ".claude" / "skills"
    else:
        skills_dir = Path.home() / ".claude" / "skills"
    entry = skills_dir / name
    if entry.is_symlink():
        target = entry.resolve()
    else:
        target = entry

    disabled_md = target / "SKILL.md.disabled" if target.is_dir() else None
    if disabled_md and disabled_md.exists():
        disabled_md.rename(target / "SKILL.md")
        disabled = load_disabled_skills()
        disabled.discard(name)
        save_disabled_skills(disabled)
        return True
    return False


def is_skill_disabled(name: str) -> bool:
    """Check if a skill is disabled."""
    return name in load_disabled_skills()


def estimate_text_tokens(text_length: int) -> int:
    """Estimate token count from text length in characters."""
    return max(1, text_length // CHARS_PER_TOKEN)


def apply_changes(
    store: dict,
    user_mcps: set[str],
    project_mcps: set[str],
    cwd: Path,
    claude_json: Path = CLAUDE_JSON,
) -> str:
    """Write user-scope and project-scope MCPs independently. Returns status msg."""
    # User scope: update ~/.claude.json mcpServers
    claude_data = load_claude_json(claude_json)
    claude_data["mcpServers"] = {n: store[n] for n in user_mcps if n in store}
    save_claude_json(claude_data, claude_json)

    # Project scope: only write .mcp.json if there are project MCPs or file already exists
    mcp_file = cwd / ".mcp.json"
    if project_mcps or mcp_file.exists():
        save_project_mcp(cwd, {"mcpServers": {n: store[n] for n in project_mcps if n in store}})

    parts = []
    parts.append(f"U: {len(user_mcps)} in ~/.claude.json")
    if project_mcps or mcp_file.exists():
        parts.append(f"P: {len(project_mcps)} in .mcp.json")
    return "Saved  |  ".join([""] + parts)
