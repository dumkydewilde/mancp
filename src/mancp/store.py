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

    return readonly


def categorize_readonly(readonly: dict[str, str]) -> dict[str, dict[str, str]]:
    """Split readonly MCPs into categories for display.

    Returns dict of {category: {name: status}}.
    """
    cats: dict[str, dict[str, str]] = {
        "cloud": {},
        "plugin": {},
        "user_mcp": {},
    }
    for name, status in readonly.items():
        if name.startswith("claude.ai"):
            cats["cloud"][name] = status
        elif name.startswith("plugin:"):
            cats["plugin"][name] = status
        else:
            cats["user_mcp"][name] = status
    return {k: v for k, v in cats.items() if v}


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
