"""Textual TUI for mancp."""

import json
from pathlib import Path
from threading import Thread

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Label, Static

from mancp.registry import (
    DiscoverServer,
    MCPServerResult,
    Skill,
    fetch_categories,
    fetch_category_servers,
    fetch_skill_description,
    fetch_skills_discover,
    fetch_tool_counts_for_configs,
    get_category_total,
    get_installed_skills,
    is_in_store,
    normalize_server_name,
    search_all,
    search_skills,
)
from mancp.store import (
    apply_plugin_changes,
    categorize_readonly,
    collect_readonly_mcps,
    count_mcp_tools,
    estimate_mcp_tokens,
    estimate_tool_count,
    get_plugins,
    get_project_scope_mcps,
    get_user_scope_mcps,
    load_settings,
    load_tool_counts_cache,
    mask_secrets,
    remove_mcp_everywhere,
    save_settings,
    save_store,
    save_tool_counts_cache,
    apply_changes,
    token_warning_level,
    tool_count_for,
    CLAUDE_DIR,
    SETTINGS_JSON,
    STORE_FILE,
)

NAME_WIDTH = 26
BADGE_WIDTH = 4  # right-aligned token badge column


def _discover_server_in_store(server: DiscoverServer, store: dict) -> bool:
    """Check if a DiscoverServer matches any entry in the store."""
    short = normalize_server_name(server.name)
    if short in store or server.name in store:
        return True
    full_pkg = f"{server.author}/{server.name}".lower() if server.author else ""
    for cfg in store.values():
        for arg in cfg.get("args", []):
            if isinstance(arg, str) and full_pkg and arg.lower() == full_pkg:
                return True
    return False


def _format_token_badge(tool_count: int) -> str:
    """Format a right-aligned token estimate badge with color."""
    if tool_count <= 0:
        return f"[#6b7280]{'·':>{BADGE_WIDTH}}[/]"
    tokens = estimate_mcp_tokens(tool_count)
    level = token_warning_level(tokens)
    tk = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
    padded = tk.rjust(BADGE_WIDTH)
    if level == "high":
        return f"[#ef4444]{padded}[/]"
    if level == "medium":
        return f"[#d97706]{padded}[/]"
    return f"[#6b7280]{padded}[/]"


class MCPRow(Widget):
    """Single row: name + independent U and P toggles."""

    can_focus = False

    focused: reactive[bool] = reactive(False)
    in_user: reactive[bool] = reactive(False)
    in_project: reactive[bool] = reactive(False)

    def __init__(
        self,
        name: str,
        cfg: dict,
        in_user: bool = False,
        in_project: bool = False,
        tool_count: int = 0,
    ):
        super().__init__()
        self.mcp_name = name
        self.mcp_cfg = cfg
        self.in_user = in_user
        self.in_project = in_project
        self.tool_count = tool_count

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        u = "[#10b981]✓[/]" if self.in_user else "[#6b7280]✗[/]"
        p = "[#10b981]✓[/]" if self.in_project else "[#6b7280]✗[/]"
        badge = _format_token_badge(self.tool_count)

        desc = self.mcp_cfg.get("command", self.mcp_cfg.get("url", ""))
        args = self.mcp_cfg.get("args", [])
        if args:
            desc += "  " + " ".join(str(a) for a in args[:3])
        desc = desc[:46]

        padded_name = self.mcp_name.ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        return f"{cursor}{u}  {p}  {padded_name} {badge}  [dim]{desc}[/dim]"

    def toggle_user(self):
        self.in_user = not self.in_user

    def toggle_project(self):
        self.in_project = not self.in_project


class ReadOnlyRow(Widget):
    """Read-only row for claude.ai connectors and plugins."""

    can_focus = False

    def __init__(self, name: str, status: str, tool_count: int = 0):
        super().__init__()
        self.mcp_name = name
        self.status = status
        self.tool_count = tool_count

    def render(self) -> str:
        if self.status == "connected":
            indicator = "[#10b981]✓[/]"
        elif self.status == "needs auth":
            indicator = "[#d97706]⚠[/]"
        elif "disabled" in self.status:
            indicator = "[#6b7280]·[/]"
        else:
            indicator = "[#6b7280]·[/]"

        badge = _format_token_badge(self.tool_count)
        padded_name = f"[#6b7280]{self.mcp_name.ljust(NAME_WIDTH)}[/]"
        return f"  {indicator}     {padded_name} {badge}  [dim]{self.status}[/dim]"


class PluginRow(Widget):
    """Toggleable row for plugins."""

    can_focus = False

    focused: reactive[bool] = reactive(False)
    enabled: reactive[bool] = reactive(False)

    def __init__(self, plugin_id: str, enabled: bool = False, tool_count: int = 0):
        super().__init__()
        self.plugin_id = plugin_id
        self.display_name = plugin_id.split("@")[0]
        self.source = plugin_id.split("@")[1] if "@" in plugin_id else ""
        self.enabled = enabled
        self.tool_count = tool_count

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        indicator = "[#10b981]✓[/]" if self.enabled else "[#6b7280]✗[/]"
        badge = _format_token_badge(self.tool_count)

        padded_name = self.display_name.ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        return f"{cursor}{indicator}     {padded_name} {badge}  [dim]{self.source}[/dim]"

    def toggle_enabled(self):
        self.enabled = not self.enabled


class SearchResultRow(Widget):
    """A single search result row."""

    can_focus = False

    focused: reactive[bool] = reactive(False)

    def __init__(self, result: MCPServerResult, index: int, in_store: bool = False):
        super().__init__()
        self.result = result
        self.index = index
        self.in_store = in_store

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        installed = "[#10b981]✓[/]" if self.in_store else "[#6b7280]·[/]"
        r = self.result

        padded_name = r.name.ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        # Show transport type badge
        if r.transport:
            short_transport = r.transport.replace("streamable-", "s-")
            transport_badge = f"[dim]{short_transport:>7}[/]"
        elif r.stars:
            transport_badge = f"[dim]{r.stars:>4}★  [/]"
        else:
            transport_badge = f"{'':>7}"

        # Show registry name for registry results, package for github
        if r.registry_name:
            meta = f"[dim]{r.registry_name}[/]"
        else:
            meta = f"[dim]{r.description[:38]}[/]"

        return f"{cursor}{installed}  {padded_name} {transport_badge}  {meta}"


class DiscoverRow(Widget):
    """A single row in the discover tab."""

    can_focus = False

    focused: reactive[bool] = reactive(False)

    def __init__(self, server: DiscoverServer, index: int, in_store: bool = False):
        super().__init__()
        self.server = server
        self.index = index
        self.in_store = in_store

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        installed = "[#10b981]✓[/]" if self.in_store else "[#6b7280]·[/]"
        padded_name = self.server.name[:NAME_WIDTH].ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        if self.server.stars:
            stars = f"[dim]{self.server.stars:>5} ★[/]"
        else:
            stars = f"{'':>7}"
        lang = self.server.language[:4] if self.server.language else ""
        lang_col = f"[dim]{lang:>4}[/]"
        desc = self.server.description[:38]
        return f"{cursor}{installed}  {padded_name} {stars} {lang_col}  [dim]{desc}[/dim]"


class CategoryRow(Widget):
    """A category row in the discover tab."""

    can_focus = False

    focused: reactive[bool] = reactive(False)

    def __init__(self, name: str, count: int, index: int):
        super().__init__()
        self.cat_name = name
        self.cat_count = count
        self.index = index

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        padded_name = self.cat_name[:NAME_WIDTH].ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        count_str = f"[dim]{self.cat_count:>4} servers[/]"
        return f"{cursor}  {padded_name} {count_str}"


class MoreRow(Widget):
    """A 'more...' row for loading additional results."""

    can_focus = False

    focused: reactive[bool] = reactive(False)

    def __init__(self, label: str = "more...", section: str = ""):
        super().__init__()
        self.label = label
        self.section = section  # "category"

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        if self.focused:
            return f"{cursor}  [bold]{self.label}[/bold]"
        return f"{cursor}  [#6b7280]{self.label}[/]"


class SkillRow(Widget):
    """A single row in the skills tab."""

    can_focus = False

    focused: reactive[bool] = reactive(False)

    def __init__(self, skill_name: str, source: str, installs: int = 0, installed: bool = False, description: str = ""):
        super().__init__()
        self.skill_name = skill_name
        self.source = source
        self.installs = installs
        self.installed = installed
        self.description = description

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        indicator = "[#10b981]✓[/]" if self.installed else "[#6b7280]·[/]"
        padded_name = self.skill_name[:NAME_WIDTH].ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        if self.installs:
            if self.installs >= 1000:
                inst = f"{self.installs / 1000:.1f}K"
            else:
                inst = str(self.installs)
            inst_col = f"[dim]{inst:>6}[/]"
        else:
            inst_col = f"{'':>6}"

        meta = self.description if self.description else self.source
        return f"{cursor}{indicator}  {padded_name} {inst_col}  [dim]{meta[:60]}[/dim]"


class SkillDetailScreen(ModalScreen):
    """Detail view for a skill."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("a", "add", "Add skill"),
        Binding("d", "delete", "Remove skill"),
        Binding("o", "open_link", "Open in browser"),
    ]

    def __init__(self, skill_name: str, source: str, installs: int = 0, installed: bool = False, description: str = ""):
        super().__init__()
        self.skill_name = skill_name
        self.source = source
        self.installs = installs
        self.is_installed = installed
        self.description = description

    def compose(self) -> ComposeResult:
        github_url = f"https://github.com/{self.source}" if "/" in self.source else ""
        lines = [
            f"[bold]{self.skill_name}[/bold]",
            "",
            f"  [dim]Source:[/dim]    {self.source}",
        ]
        if github_url:
            lines.append(f"  [dim]GitHub:[/dim]    {github_url}")
        if self.installs:
            lines.append(f"  [dim]Installs:[/dim]  {self.installs:,}")
        if self.description:
            lines += ["", f"  {self.description}"]
        if not self.is_installed and "/" in self.source:
            lines += [
                "",
                f"  [dim]Install:[/dim]   npx skills add --global {self.source}",
            ]
        lines += [
            "",
            "[dim]──────────────────────────────────────────────[/dim]",
        ]
        if self.is_installed:
            lines.append("  [#10b981]Installed[/]  —  [bold]D[/bold]  remove  —  [bold]O[/bold]  open in browser")
        else:
            lines.append("  [bold]A[/bold]  install  —  [bold]O[/bold]  open in browser")
        lines.append("  [bold]Q / Esc[/bold]  close")

        with ScrollableContainer(id="skill-detail-box"):
            yield Label("\n".join(lines), id="skill-detail-content")
            if not self.description:
                yield Static("  [dim]Loading description...[/dim]", id="skill-readme")

    def on_mount(self):
        if not self.description and "/" in self.source:
            thread = Thread(target=self._fetch_description, daemon=True)
            thread.start()

    def _fetch_description(self):
        desc = fetch_skill_description(self.source, self.skill_name)
        try:
            self.app.call_from_thread(self._show_description, desc)
        except Exception:
            pass  # Screen was dismissed before thread finished

    def _show_description(self, desc: str):
        try:
            widget = self.query_one("#skill-readme", Static)
        except Exception:
            return
        if desc:
            widget.update(f"\n  {desc}")
        else:
            widget.update("")

    def action_close(self):
        self.dismiss(None)

    def action_add(self):
        if not self.is_installed:
            self.dismiss("add")

    def action_delete(self):
        if self.is_installed:
            self.dismiss("delete")

    def action_open_link(self):
        if "/" in self.source:
            import webbrowser
            webbrowser.open(f"https://github.com/{self.source}")

    DEFAULT_CSS = """
    SkillDetailScreen {
        align: center middle;
    }
    #skill-detail-box {
        width: 72;
        max-height: 85%;
        border: solid $accent;
        padding: 1 2;
    }
    #skill-detail-content {
        width: 100%;
        height: auto;
    }
    #skill-readme {
        width: 100%;
        height: auto;
    }
    """


class ConfirmScreen(ModalScreen):
    """Yes/no confirmation dialog."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n,escape,q", "cancel", "No"),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(
                f"{self.message}\n\n"
                "  [bold]Y[/bold]  yes   [bold]N / Esc[/bold]  no",
                id="confirm-content",
            )

    def action_confirm(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 50;
        height: auto;
        border: solid $accent;
        padding: 1 2;
    }
    #confirm-content {
        width: 100%;
    }
    """


class DetailScreen(ModalScreen):
    """Detail view for a single MCP."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("d", "delete", "Delete from store"),
    ]

    def __init__(self, name: str, cfg: dict):
        super().__init__()
        self.mcp_name = name
        self.mcp_cfg = cfg

    def compose(self) -> ComposeResult:
        masked = mask_secrets(self.mcp_cfg)
        pretty = json.dumps(masked, indent=2)

        lines = [
            f"[bold]{self.mcp_name}[/bold]",
            "",
            "[dim]Configuration (secrets masked):[/dim]",
            "",
        ]
        for line in pretty.splitlines():
            lines.append(f"  {line}")
        lines += [
            "",
            "[dim]─────────────────────────────────────────[/dim]",
            "  [bold]D[/bold]  delete from store",
            "  [bold]Q / Esc[/bold]  close",
        ]

        with Vertical(id="detail-box"):
            yield Label("\n".join(lines), id="detail-content")

    def action_close(self):
        self.dismiss(None)

    def action_delete(self):
        self.dismiss("delete")

    DEFAULT_CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail-box {
        width: 70;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        padding: 1 2;
    }
    #detail-content {
        width: 100%;
    }
    """


class SearchResultDetailScreen(ModalScreen):
    """Detail view for a search result with option to add to store."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("a", "add", "Add to store"),
    ]

    def __init__(self, result: MCPServerResult, in_store: bool = False):
        super().__init__()
        self.result = result
        self.already_in_store = in_store

    def compose(self) -> ComposeResult:
        r = self.result
        source_label = {
            "github_mcp": "GitHub (MCP org)",
            "registry": "MCP Registry",
        }.get(r.source, r.source)

        lines = [
            f"[bold]{r.name}[/bold]",
            "",
            f"  [dim]Source:[/dim]   {source_label}",
            f"  [dim]Package:[/dim]  {r.package}",
        ]
        if r.version:
            lines.append(f"  [dim]Version:[/dim]  {r.version}")
        if r.url:
            lines.append(f"  [dim]URL:[/dim]      {r.url}")
        if r.stars:
            lines.append(f"  [dim]Stats:[/dim]    ★ {r.stars:,}")
        if r.author:
            lines.append(f"  [dim]Author:[/dim]   {r.author}")
        if r.license:
            lines.append(f"  [dim]License:[/dim]  {r.license}")
        lines += [
            "",
            f"  {r.description}",
        ]
        if r.install_hint:
            lines += ["", f"  [dim]Install:[/dim]  {r.install_hint}"]

        cfg = r.to_mcp_config()
        lines += [
            "",
            "  [dim]MCP config:[/dim]",
            f"    {json.dumps(cfg)}",
            "",
            "[dim]─────────────────────────────────────────────────[/dim]",
        ]
        if self.already_in_store:
            lines.append("  [#10b981]Already in store[/]")
        else:
            lines.append("  [bold]A[/bold]  add to store")
        lines.append("  [bold]Q / Esc[/bold]  close")

        with Vertical(id="search-detail-box"):
            yield Label("\n".join(lines), id="search-detail-content")

    def action_close(self):
        self.dismiss(None)

    def action_add(self):
        if not self.already_in_store:
            self.dismiss("add")

    DEFAULT_CSS = """
    SearchResultDetailScreen {
        align: center middle;
    }
    #search-detail-box {
        width: 72;
        height: auto;
        max-height: 85%;
        border: solid $accent;
        padding: 1 2;
    }
    #search-detail-content {
        width: 100%;
    }
    """


class DiscoverDetailScreen(ModalScreen):
    """Detail view for a discover server with option to add to store."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("a", "add", "Add to store"),
    ]

    def __init__(self, server: DiscoverServer):
        super().__init__()
        self.server = server

    def compose(self) -> ComposeResult:
        s = self.server
        lines = [
            f"[bold]{s.name}[/bold]",
            "",
        ]
        if s.author:
            lines.append(f"  [dim]Author:[/dim]     {s.author}")
        if s.url:
            lines.append(f"  [dim]URL:[/dim]        {s.url}")
        # Stars and forks on one line
        stats = []
        if s.stars:
            stats.append(f"★ {s.stars:,}")
        if s.forks:
            stats.append(f"⑂ {s.forks:,}")
        if stats:
            lines.append(f"  [dim]Stats:[/dim]      {'   '.join(stats)}")
        if s.language:
            lines.append(f"  [dim]Language:[/dim]   {s.language}")
        if s.license:
            lines.append(f"  [dim]License:[/dim]    {s.license}")
        if s.category:
            lines.append(f"  [dim]Category:[/dim]   {s.category}")
        lines += [
            "",
            f"  {s.description}",
            "",
            "[dim]──────────────────────────────────────────────[/dim]",
            "  [bold]A[/bold]  add to store (will use npx with package name)",
            "  [bold]Q / Esc[/bold]  close",
        ]

        with Vertical(id="discover-detail-box"):
            yield Label("\n".join(lines), id="discover-detail-content")

    def action_close(self):
        self.dismiss(None)

    def action_add(self):
        self.dismiss("add")

    DEFAULT_CSS = """
    DiscoverDetailScreen {
        align: center middle;
    }
    #discover-detail-box {
        width: 72;
        height: auto;
        max-height: 85%;
        border: solid $accent;
        padding: 1 2;
    }
    #discover-detail-content {
        width: 100%;
    }
    """


class ManCPApp(App):

    CSS = """
    #mcp-list {
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    #search-panel {
        height: 1fr;
        padding: 0 1;
    }
    #search-input {
        margin: 0 1 1 1;
    }
    #search-col-header {
        height: 1;
        padding: 0 1;
    }
    #search-status {
        height: 1;
        padding: 0 1;
    }
    #search-results {
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    #discover-panel {
        height: 1fr;
        padding: 0 1;
    }
    #discover-status {
        height: 1;
        padding: 0 1;
    }
    #discover-col-header {
        height: 1;
        padding: 0 1;
    }
    #discover-results {
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    MCPRow {
        height: 1;
    }
    ReadOnlyRow {
        height: 1;
    }
    PluginRow {
        height: 1;
    }
    SearchResultRow {
        height: 1;
    }
    DiscoverRow {
        height: 1;
    }
    CategoryRow {
        height: 1;
    }
    MoreRow {
        height: 1;
    }
    SkillRow {
        height: 1;
    }
    #skills-panel {
        height: 1fr;
        padding: 0 1;
    }
    #skills-search-input {
        margin: 0 1 1 1;
    }
    #skills-status {
        height: 1;
        padding: 0 1;
    }
    #skills-results {
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    .readonly-hint {
        height: 1;
        padding: 0 1;
    }
    .readonly-header {
        height: 1;
        padding: 0 1;
        margin-top: 1;
    }
    #tab-bar {
        height: 1;
        padding: 0 1;
        margin-bottom: 1;
    }
    #col-header {
        height: 1;
        padding: 0 1;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
    }
    #footer-hints {
        height: 1;
        padding: 0 1;
        margin-top: 1;
        border-top: solid #444444;
    }
    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("up,k", "move_up", "Up", show=False),
        Binding("down,j", "move_down", "Down", show=False),
        Binding("u", "toggle_user", "Toggle user scope", show=False),
        Binding("p", "toggle_project", "Toggle project scope", show=False),
        Binding("e", "toggle_enabled", "Toggle plugin enabled", show=False),
        Binding("enter", "select_or_detail", "Select/Detail", show=False),
        Binding("d", "delete", "Delete from store", show=False),
        Binding("s", "apply", "Save", show=False),
        Binding("ctrl+s", "apply", "Save", show=False),
        Binding("q,escape", "quit_app", "Quit", show=False),
    ]

    TABS = ["servers", "search", "discover", "skills"]

    def __init__(self, store: dict, cwd: Path, store_file=None, claude_json=None, claude_dir=None, settings_json=None):
        super().__init__()
        self.theme = "textual-ansi"
        self.store = store
        self.cwd = cwd
        self.store_file = store_file or STORE_FILE
        self.claude_json = claude_json
        self.claude_dir = claude_dir or CLAUDE_DIR
        self.settings_json = settings_json or (self.claude_dir / "settings.json")
        kwargs = {"claude_json": claude_json} if claude_json else {}
        self.user_active = get_user_scope_mcps(**kwargs)
        self.project_active = get_project_scope_mcps(cwd)
        self.plugins = get_plugins(self.settings_json)
        self.readonly_mcps = collect_readonly_mcps(self.claude_dir)
        self.tool_counts = count_mcp_tools(self.claude_dir)
        self.cached_tool_counts = load_tool_counts_cache()
        self.names = sorted(store.keys())
        self.plugin_ids = sorted(self.plugins.keys())
        self.cursor = 0
        self.active_tab = "servers"
        self.search_cursor = 0
        self.search_results: list[MCPServerResult] = []
        self.discover_cursor = 0
        self.discover_servers: list[DiscoverServer] = []
        self.discover_view = "categories"  # "categories" or "servers"
        self.discover_category: str = ""
        self.discover_cat_offset: int = 0  # pagination offset for category
        self.discover_cat_total: int = 0  # total entries in category
        self._input_focused = False
        self.skills_cursor = 0
        self.skills_loaded = False

    def compose(self) -> ComposeResult:
        cwd_name = self.cwd.name
        yield Label(self._render_tab_bar(cwd_name), id="tab-bar")

        # -- Servers tab --
        yield Label(
            "  [bold]U[/]  [bold]P[/]  name                        [bold]ctx[/]  command / url",
            id="col-header",
        )
        with ScrollableContainer(id="mcp-list", can_focus=False):
            for i, name in enumerate(self.names):
                cfg = self.store[name]
                row = MCPRow(
                    name=name,
                    cfg=cfg,
                    in_user=(name in self.user_active),
                    in_project=(name in self.project_active),
                    tool_count=self._tool_count_for(name, cfg),
                )
                row.focused = i == self.cursor
                yield row
            yield from self._plugin_widgets()
            yield from self._readonly_widgets()

        # -- Search tab (hidden initially) --
        with Vertical(id="search-panel", classes="hidden"):
            yield Input(placeholder="Search MCP servers (GitHub MCP org + Registry)...", id="search-input", disabled=True)
            yield Label(
                "  [bold]✓[/]  name                       [bold]type[/]   registry / description",
                id="search-col-header",
            )
            yield Static("  [dim]Type a query and press Enter to search[/dim]", id="search-status")
            yield ScrollableContainer(id="search-results", can_focus=False)

        # -- Discover tab (hidden initially) --
        with Vertical(id="discover-panel", classes="hidden"):
            yield Static("  [bold]Discover MCP servers[/bold]", id="discover-status")
            yield Label(
                f"     {'name':<{NAME_WIDTH}}     [bold]★[/]  [bold]lang[/]  description",
                id="discover-col-header",
            )
            yield ScrollableContainer(id="discover-results", can_focus=False)

        # -- Skills tab (hidden initially) --
        with Vertical(id="skills-panel", classes="hidden"):
            yield Input(placeholder="Search skills (skills.sh)...", id="skills-search-input", disabled=True)
            yield Static("  [dim]Loading skills...[/dim]", id="skills-status")
            yield ScrollableContainer(id="skills-results", can_focus=False)

        yield Static("", id="status-bar")
        yield Label(self._footer_for_tab(), id="footer-hints")

    def on_mount(self):
        # Fetch tool counts from source repos in background for servers without counts
        missing = {
            name: cfg for name, cfg in self.store.items()
            if self._tool_count_for(name, cfg) == 0
        }
        if missing:
            thread = Thread(target=self._fetch_tool_counts, args=(missing,), daemon=True)
            thread.start()

    def _fetch_tool_counts(self, configs: dict[str, dict]):
        new_counts = fetch_tool_counts_for_configs(configs)
        if new_counts:
            merged = {**self.cached_tool_counts, **new_counts}
            save_tool_counts_cache(merged)
            try:
                self.call_from_thread(self._apply_fetched_counts, merged)
            except Exception:
                pass

    def _apply_fetched_counts(self, counts: dict[str, int]):
        self.cached_tool_counts = counts
        for row in self.query(MCPRow):
            tc = self._tool_count_for(row.mcp_name, row.mcp_cfg)
            if tc != row.tool_count:
                row.tool_count = tc
                row.refresh()

    def _render_tab_bar(self, cwd_name: str | None = None) -> str:
        if cwd_name is None:
            cwd_name = self.cwd.name
        store_count = len(self.store)
        tabs = []
        for t in self.TABS:
            if t == self.active_tab:
                tabs.append(f"[bold reverse] {t} [/]")
            else:
                tabs.append(f"[dim] {t} [/]")
        tab_str = "  ".join(tabs)
        return f"  [bold]mancp[/bold]  {cwd_name}  [dim]{store_count} in store[/dim]   {tab_str}"

    def _footer_for_tab(self) -> str:
        if self.active_tab == "servers":
            return (
                "  [dim]j/k[/dim] navigate   [dim]u[/dim] user   [dim]p[/dim] project   "
                "[dim]e[/dim] enable   [dim]enter[/dim] detail   [dim]d[/dim] delete   "
                "[dim]s[/dim] save   [dim]→[/dim] search   [dim]q[/dim] quit"
            )
        if self.active_tab == "search":
            return (
                "  [dim]type[/dim] to search   [dim]tab[/dim] results   "
                "[dim]j/k[/dim] navigate   [dim]enter[/dim] detail   "
                "[dim]←[/dim] servers   [dim]→[/dim] discover   [dim]q[/dim] quit"
            )
        if self.active_tab == "skills":
            return (
                "  [dim]type[/dim] to search   [dim]tab[/dim] results   "
                "[dim]j/k[/dim] navigate   [dim]enter[/dim] detail   "
                "[dim]d[/dim] remove   [dim]←[/dim] discover   [dim]q[/dim] quit"
            )
        # discover
        if self.discover_view == "categories":
            return (
                "  [dim]j/k[/dim] navigate   [dim]enter[/dim] select   "
                "[dim]r[/dim] refresh   [dim]←[/dim] search   [dim]→[/dim] skills   [dim]q[/dim] quit"
            )
        return (
            "  [dim]j/k[/dim] navigate   [dim]enter[/dim] detail   "
            "[dim]esc[/dim] back   [dim]←[/dim] search   [dim]→[/dim] skills   [dim]q[/dim] quit"
        )

    def _switch_tab(self, tab: str):
        if tab == self.active_tab:
            return
        self.active_tab = tab

        mcp_list = self.query_one("#mcp-list", ScrollableContainer)
        col_header = self.query_one("#col-header", Label)
        search_panel = self.query_one("#search-panel", Vertical)
        discover_panel = self.query_one("#discover-panel", Vertical)
        skills_panel = self.query_one("#skills-panel", Vertical)
        search_input = self.query_one("#search-input", Input)
        skills_search_input = self.query_one("#skills-search-input", Input)

        # Hide all
        mcp_list.add_class("hidden")
        col_header.add_class("hidden")
        search_panel.add_class("hidden")
        discover_panel.add_class("hidden")
        skills_panel.add_class("hidden")
        if search_input.has_focus:
            search_input.blur()
        search_input.disabled = True
        if skills_search_input.has_focus:
            skills_search_input.blur()
        skills_search_input.disabled = True
        self._input_focused = False

        if tab == "servers":
            mcp_list.remove_class("hidden")
            col_header.remove_class("hidden")
        elif tab == "search":
            search_panel.remove_class("hidden")
            search_input.disabled = False
            search_input.focus()
            self._input_focused = True
        elif tab == "discover":
            discover_panel.remove_class("hidden")
            if not self.query(CategoryRow) and not self.query(DiscoverRow):
                self._show_categories()
        elif tab == "skills":
            skills_panel.remove_class("hidden")
            skills_search_input.disabled = False
            skills_search_input.focus()
            self._input_focused = True
            if not self.skills_loaded:
                self._load_skills()

        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        self.query_one("#footer-hints", Label).update(self._footer_for_tab())

    # -- Discover tab --

    def _show_categories(self):
        """Show category list from awesome-mcp-servers."""
        self.discover_view = "categories"
        self.discover_cursor = 0
        container = self.query_one("#discover-results", ScrollableContainer)
        container.remove_children()

        self.query_one("#discover-status", Static).update(
            "  [bold]Discover MCP servers[/bold]"
        )

        categories = fetch_categories()
        for idx, (cat_name, cat_count) in enumerate(categories):
            row = CategoryRow(name=cat_name, count=cat_count, index=idx)
            container.mount(row)

        self._set_discover_cursor(0)
        self.query_one("#footer-hints", Label).update(self._footer_for_tab())

    def _navigable_discover_rows(self) -> list[DiscoverRow | CategoryRow | MoreRow]:
        """All navigable rows in discover tab, in DOM order."""
        container = self.query_one("#discover-results", ScrollableContainer)
        return [
            w for w in container.children
            if isinstance(w, (DiscoverRow, CategoryRow, MoreRow))
        ]

    def _set_discover_cursor(self, idx: int):
        rows = self._navigable_discover_rows()
        if not rows:
            return
        idx = max(0, min(idx, len(rows) - 1))
        # Clear all focused states to prevent ghost cursors after async inserts
        for row in rows:
            row.focused = False
        self.discover_cursor = idx
        rows[self.discover_cursor].focused = True
        rows[self.discover_cursor].scroll_visible()

    def _discover_enter(self):
        """Handle Enter in discover tab."""
        rows = self._navigable_discover_rows()
        if not rows or self.discover_cursor >= len(rows):
            return
        current = rows[self.discover_cursor]
        if isinstance(current, CategoryRow):
            self._open_category(current.cat_name)
        elif isinstance(current, MoreRow):
            self._load_more(current.section)
        elif isinstance(current, DiscoverRow):
            self._show_discover_detail()

    def _open_category(self, category: str):
        """Load servers for a category."""
        self.discover_category = category
        self.discover_view = "servers"
        self.discover_cursor = 0
        self.discover_cat_offset = 0
        self.discover_cat_total = get_category_total(category)
        container = self.query_one("#discover-results", ScrollableContainer)
        container.remove_children()
        self.query_one("#discover-status", Static).update(
            f"  [bold]{category}[/bold]  [dim]loading...[/dim]"
        )
        self.query_one("#footer-hints", Label).update(self._footer_for_tab())
        thread = Thread(target=self._do_load_category, args=(category, 0), daemon=True)
        thread.start()

    def _do_load_category(self, category: str, offset: int):
        servers = fetch_category_servers(category, offset=offset)
        self.call_from_thread(self._show_category_servers, category, servers, offset)

    def _show_category_servers(self, category: str, servers: list[DiscoverServer], offset: int):
        self.discover_servers = servers
        self.discover_cat_offset = offset + len(servers)
        self.discover_cursor = 0
        container = self.query_one("#discover-results", ScrollableContainer)
        container.remove_children()

        if not servers:
            self.query_one("#discover-status", Static).update(
                f"  [bold]{category}[/bold]  [dim]no servers found[/dim]"
            )
            return

        self.query_one("#discover-status", Static).update(
            f"  [bold]{category}[/bold]  [dim]({len(servers)}/{self.discover_cat_total} servers)[/dim]"
        )

        for i, server in enumerate(servers):
            row = DiscoverRow(server=server, index=i, in_store=_discover_server_in_store(server, self.store))
            row.focused = i == 0
            container.mount(row)

        if self.discover_cat_offset < self.discover_cat_total:
            container.mount(MoreRow(label="more...", section="category"))

    def _load_more(self, section: str):
        """Load more results for a section."""
        if section == "category":
            self._load_more_category()

    def _load_more_category(self):
        """Load next page of category servers from GitHub."""
        if self.discover_cat_offset >= self.discover_cat_total:
            return
        category = self.discover_category
        offset = self.discover_cat_offset
        # Remove the "more..." row
        for w in self.query(MoreRow):
            if w.section == "category":
                w.remove()
                break
        self.query_one("#discover-status", Static).update(
            f"  [bold]{category}[/bold]  [dim]loading more...[/dim]"
        )
        thread = Thread(
            target=self._do_load_more_category, args=(category, offset), daemon=True
        )
        thread.start()

    def _do_load_more_category(self, category: str, offset: int):
        servers = fetch_category_servers(category, offset=offset)
        self.call_from_thread(self._append_category_servers, category, servers, offset)

    def _append_category_servers(self, category: str, servers: list[DiscoverServer], offset: int):
        self.discover_servers.extend(servers)
        self.discover_cat_offset = offset + len(servers)
        container = self.query_one("#discover-results", ScrollableContainer)

        self.query_one("#discover-status", Static).update(
            f"  [bold]{category}[/bold]  [dim]({len(self.discover_servers)}/{self.discover_cat_total} servers)[/dim]"
        )

        for server in servers:
            row = DiscoverRow(server=server, index=-1, in_store=_discover_server_in_store(server, self.store))
            container.mount(row)

        if self.discover_cat_offset < self.discover_cat_total:
            container.mount(MoreRow(label="more...", section="category"))

    def _discover_back(self):
        """Go back from category server list to categories."""
        if self.discover_view == "servers":
            self._show_categories()
        else:
            self._switch_tab("search")

    def _show_discover_detail(self):
        rows = self._navigable_discover_rows()
        if not rows or self.discover_cursor >= len(rows):
            return
        current = rows[self.discover_cursor]
        if not isinstance(current, DiscoverRow):
            return
        server = current.server

        def handle_result(result):
            if result == "add":
                self._add_discover_server(server)

        self.push_screen(DiscoverDetailScreen(server), handle_result)

    def _add_discover_server(self, server: DiscoverServer):
        # Derive short name and package from the GitHub URL
        # URL format: https://github.com/owner/repo
        short = normalize_server_name(server.name)
        author = server.author

        if short in self.store:
            self.query_one("#status-bar", Static).update(f"  '{short}' already in store")
            return

        full_pkg = f"{author}/{server.name}" if author else server.name
        cfg = {"command": "npx", "args": ["-y", full_pkg]}
        self.store[short] = cfg
        save_store(self.store, self.store_file)
        self.names = sorted(self.store.keys())
        self._rebuild_list()
        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        self.query_one("#status-bar", Static).update(
            f"  + {short} ({full_pkg}) added to store"
        )
        # Mark the discover row as installed
        rows = self._navigable_discover_rows()
        if rows and self.discover_cursor < len(rows):
            current = rows[self.discover_cursor]
            if isinstance(current, DiscoverRow) and current.server is server:
                current.in_store = True
                current.refresh()

    # -- Skills tab --

    def _load_skills(self, status_override: str = ""):
        """Load installed skills and discover skills in background."""
        self.skills_loaded = True
        self._skills_status_override = status_override
        if not status_override:
            self.query_one("#skills-status", Static).update("  [dim]Loading skills from skills.sh...[/dim]")
        thread = Thread(target=self._do_load_skills, daemon=True)
        thread.start()

    def _do_load_skills(self):
        installed = get_installed_skills()
        discover = fetch_skills_discover(count=25)
        self.call_from_thread(self._show_skills, installed, discover)

    def _show_skills(self, installed: list[dict], discover: list[Skill]):
        installed_names = {s["name"] for s in installed}
        self._skills_installed_names = installed_names
        container = self.query_one("#skills-results", ScrollableContainer)
        container.remove_children()
        self.skills_cursor = 0

        # Installed skills section
        self._all_installed_skills = installed
        INSTALLED_INITIAL = 5
        if installed:
            container.mount(Label(
                f" [dim]── installed ({len(installed)}) ──[/dim]",
                classes="readonly-header",
            ))
            for s in installed[:INSTALLED_INITIAL]:
                row = SkillRow(
                    skill_name=s["name"],
                    source="local",
                    installed=True,
                    description=s["description"],
                )
                container.mount(row)
            if len(installed) > INSTALLED_INITIAL:
                container.mount(MoreRow(
                    label=f"more... ({len(installed) - INSTALLED_INITIAL} hidden)",
                    section="installed",
                ))

        # Discover section
        if discover:
            container.mount(Label(
                f" [dim]── discover (skills.sh trending + all time) ──[/dim]",
                classes="readonly-header",
            ))
            for s in discover:
                row = SkillRow(
                    skill_name=s.name,
                    source=s.source,
                    installs=s.installs,
                    installed=s.name in installed_names,
                )
                container.mount(row)
            # "more..." row to load additional skills
            container.mount(MoreRow(label="more...", section="skills"))

        status = getattr(self, "_skills_status_override", "")
        if status:
            self.query_one("#skills-status", Static).update(status)
            self._skills_status_override = ""
        else:
            self.query_one("#skills-status", Static).update(
                f"  [bold]Skills[/bold]  [dim]{len(installed)} installed, {len(discover)} to discover[/dim]"
            )
        self._set_skills_cursor(0)

    def _do_search_skills(self, query: str):
        results = search_skills(query)
        installed = get_installed_skills()
        self.call_from_thread(self._show_skills_search_results, results, installed)

    def _show_skills_search_results(self, results: list[Skill], installed: list[dict]):
        installed_names = {s["name"] for s in installed}
        container = self.query_one("#skills-results", ScrollableContainer)
        container.remove_children()
        self.skills_cursor = 0

        if not results:
            self.query_one("#skills-status", Static).update("  No skills found. Try a different query.")
            return

        self.query_one("#skills-status", Static).update(
            f"  [bold]{len(results)} skills found[/bold]"
        )
        for s in results[:30]:
            row = SkillRow(
                skill_name=s.name,
                source=s.source,
                installs=s.installs,
                installed=s.name in installed_names,
            )
            container.mount(row)
        self._set_skills_cursor(0)

    def _navigable_skill_rows(self) -> list[SkillRow | MoreRow]:
        container = self.query_one("#skills-results", ScrollableContainer)
        return [
            w for w in container.children
            if isinstance(w, (SkillRow, MoreRow))
        ]

    def _skill_rows(self) -> list[SkillRow]:
        return list(self.query(SkillRow))

    def _set_skills_cursor(self, idx: int):
        rows = self._navigable_skill_rows()
        if not rows:
            return
        idx = max(0, min(idx, len(rows) - 1))
        for row in rows:
            row.focused = False
        self.skills_cursor = idx
        rows[self.skills_cursor].focused = True
        rows[self.skills_cursor].scroll_visible()

    def _skills_enter(self):
        """Handle Enter in skills tab."""
        rows = self._navigable_skill_rows()
        if not rows or self.skills_cursor >= len(rows):
            return
        current = rows[self.skills_cursor]
        if isinstance(current, MoreRow):
            if current.section == "installed":
                self._expand_installed_skills()
            else:
                self._load_more_skills()
        elif isinstance(current, SkillRow):
            self._show_skill_detail()

    def _show_skill_detail(self):
        rows = self._skill_rows()
        if not rows or self.skills_cursor >= len(self._navigable_skill_rows()):
            return
        current = self._navigable_skill_rows()[self.skills_cursor]
        if not isinstance(current, SkillRow):
            return
        row = current

        def handle_result(result):
            if result == "add":
                self._install_skill(row)
            elif result == "delete":
                self._remove_skill(row)

        self.push_screen(SkillDetailScreen(
            skill_name=row.skill_name,
            source=row.source,
            installs=row.installs,
            installed=row.installed,
            description=row.description,
        ), handle_result)

    def _expand_installed_skills(self):
        """Show all installed skills (expand from initial 5)."""
        container = self.query_one("#skills-results", ScrollableContainer)
        # Remove the "more..." row for installed
        more_row = None
        for w in container.children:
            if isinstance(w, MoreRow) and w.section == "installed":
                more_row = w
                break
        if not more_row:
            return
        # Insert remaining installed skills before the more_row, then remove it
        all_installed = getattr(self, "_all_installed_skills", [])
        for s in all_installed[5:]:
            row = SkillRow(
                skill_name=s["name"],
                source="local",
                installed=True,
                description=s["description"],
            )
            container.mount(row, before=more_row)
        more_row.remove()

    def _load_more_skills(self):
        """Load more discover skills in background."""
        self.query_one("#skills-status", Static).update("  [dim]Loading more skills...[/dim]")
        # Remove the MoreRow
        for w in self.query_one("#skills-results", ScrollableContainer).children:
            if isinstance(w, MoreRow) and w.section == "skills":
                w.remove()
                break
        thread = Thread(target=self._do_load_more_skills, daemon=True)
        thread.start()

    def _do_load_more_skills(self):
        # Fetch a larger batch
        more = fetch_skills_discover(count=75)
        self.call_from_thread(self._append_more_skills, more)

    def _append_more_skills(self, all_skills: list[Skill]):
        container = self.query_one("#skills-results", ScrollableContainer)
        installed_names = getattr(self, "_skills_installed_names", set())
        # Get currently shown skill keys to avoid duplicates
        existing = {(r.skill_name, r.source) for r in self.query(SkillRow) if not r.installed}
        added = 0
        for s in all_skills:
            if (s.name, s.source) not in existing:
                row = SkillRow(
                    skill_name=s.name,
                    source=s.source,
                    installs=s.installs,
                    installed=s.name in installed_names,
                )
                container.mount(row)
                added += 1
        total_discover = len([r for r in self.query(SkillRow) if not r.installed])
        self.query_one("#skills-status", Static).update(
            f"  [bold]Skills[/bold]  [dim]{total_discover} to discover[/dim]"
        )

    def _install_skill(self, row: SkillRow):
        """Install a skill via npx skills add."""
        import subprocess
        source = row.source
        self.query_one("#skills-status", Static).update(f"  Installing {row.skill_name}...")

        def do_install():
            try:
                result = subprocess.run(
                    ["npx", "skills", "add", "--yes", "--all", "--global", source],
                    capture_output=True, text=True, timeout=60,
                )
                success = result.returncode == 0
                msg = result.stdout.strip() or result.stderr.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                success = False
                msg = str(e)
            self.call_from_thread(self._on_skill_installed, row, success, msg)

        thread = Thread(target=do_install, daemon=True)
        thread.start()

    def _on_skill_installed(self, row: SkillRow, success: bool, msg: str):
        if success:
            # Reload the full skills view to show it in the installed section
            self.skills_loaded = False
            self._load_skills(status_override=f"  [#10b981]✓[/] Installed {row.skill_name}")
        else:
            self.query_one("#skills-status", Static).update(
                f"  [#ef4444]✗[/] Failed: {msg[:60]}"
            )

    def _remove_skill(self, row: SkillRow):
        """Remove an installed skill via npx skills remove."""
        import subprocess

        def do_remove():
            try:
                result = subprocess.run(
                    ["npx", "skills", "remove", "--yes", "--global", row.skill_name],
                    capture_output=True, text=True, timeout=30,
                )
                success = result.returncode == 0
                msg = result.stdout.strip() or result.stderr.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                success = False
                msg = str(e)
            self.call_from_thread(self._on_skill_removed, row, success, msg)

        thread = Thread(target=do_remove, daemon=True)
        thread.start()

    def _on_skill_removed(self, row: SkillRow, success: bool, msg: str):
        if success:
            # Reload the full skills view to remove it from the installed section
            self.skills_loaded = False
            self._load_skills(status_override=f"  Removed {row.skill_name}")
        else:
            self.query_one("#skills-status", Static).update(
                f"  [#ef4444]✗[/] Failed: {msg[:60]}"
            )

    # -- Search input handlers --

    def on_input_submitted(self, event: Input.Submitted):
        query = event.value.strip()
        if not query:
            return
        if event.input.id == "skills-search-input":
            self.query_one("#skills-status", Static).update("  Searching skills...")
            thread = Thread(target=self._do_search_skills, args=(query,), daemon=True)
            thread.start()
            return
        self.query_one("#search-status", Static).update("  Searching...")
        thread = Thread(target=self._do_search, args=(query,), daemon=True)
        thread.start()

    def _do_search(self, query: str):
        results = search_all(query)
        self.call_from_thread(self._show_search_results, results)

    def _show_search_results(self, results: list[MCPServerResult]):
        self.search_results = results
        self.search_cursor = 0
        container = self.query_one("#search-results", ScrollableContainer)
        container.remove_children()
        if not results:
            self.query_one("#search-status", Static).update("  No results found. Try a different query.")
            return

        gh_count = sum(1 for r in results if r.source == "github_mcp")
        reg_count = len(results) - gh_count
        self.query_one("#search-status", Static).update(
            f"  {len(results)} results  [dim]({gh_count} github mcp org, {reg_count} registry)[/dim]"
        )

        shown_gh_header = False
        shown_reg_header = False
        row_idx = 0
        for r in results:
            if r.source == "github_mcp" and not shown_gh_header:
                container.mount(Label(
                    " [dim]── github.com/modelcontextprotocol ──[/dim]",
                    classes="readonly-header",
                ))
                shown_gh_header = True
            elif r.source == "registry" and not shown_reg_header:
                container.mount(Label(
                    " [dim]── registry.modelcontextprotocol.io ──[/dim]",
                    classes="readonly-header",
                ))
                shown_reg_header = True

            in_store = is_in_store(r, self.store)
            row = SearchResultRow(result=r, index=row_idx, in_store=in_store)
            row.focused = row_idx == 0
            container.mount(row)
            row_idx += 1

    def _search_result_rows(self) -> list[SearchResultRow]:
        return list(self.query(SearchResultRow))

    def _set_search_cursor(self, idx: int):
        rows = self._search_result_rows()
        if not rows:
            return
        idx = max(0, min(idx, len(rows) - 1))
        if self.search_cursor < len(rows):
            rows[self.search_cursor].focused = False
        self.search_cursor = idx
        rows[self.search_cursor].focused = True
        rows[self.search_cursor].scroll_visible()

    def _show_search_detail(self):
        """Show detail dialog for selected search result."""
        rows = self._search_result_rows()
        if not rows or self.search_cursor >= len(rows):
            return
        row = rows[self.search_cursor]
        result = row.result
        in_store = row.in_store

        def handle_result(action):
            if action == "add":
                self._add_search_result()

        self.push_screen(SearchResultDetailScreen(result, in_store=in_store), handle_result)

    def _add_search_result(self):
        rows = self._search_result_rows()
        if not rows or self.search_cursor >= len(rows):
            return
        result = rows[self.search_cursor].result
        name = result.name
        if name in self.store:
            self.query_one("#status-bar", Static).update(
                f"  '{name}' already in store"
            )
            return
        cfg = result.to_mcp_config()
        self.store[name] = cfg
        save_store(self.store, self.store_file)
        self.names = sorted(self.store.keys())
        rows[self.search_cursor].in_store = True
        rows[self.search_cursor].refresh()
        self._rebuild_list()
        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        self.query_one("#status-bar", Static).update(
            f"  + {name} ({result.package}) added to store"
        )

    def on_key(self, event):
        # Don't handle keys when a modal screen is on top
        if len(self.screen_stack) > 1:
            return

        focused = self.focused
        is_input = isinstance(focused, Input)
        self._input_focused = is_input

        # -- Servers tab --
        if self.active_tab == "servers":
            if event.key in ("right", "l"):
                self._switch_tab("search")
                event.stop()
                event.prevent_default()
            return

        # -- Search tab --
        if self.active_tab == "search":
            if is_input:
                inp = self.query_one("#search-input", Input)
                if event.key in ("tab", "down"):
                    if self._search_result_rows():
                        inp.blur()
                        self._input_focused = False
                        self._set_search_cursor(self.search_cursor)
                        event.stop()
                        event.prevent_default()
                elif event.key == "right" and not inp.value:
                    self._switch_tab("discover")
                    event.stop()
                    event.prevent_default()
                elif event.key == "left" and not inp.value:
                    self._switch_tab("servers")
                    event.stop()
                    event.prevent_default()
                elif event.key == "escape":
                    if inp.value:
                        inp.value = ""
                    else:
                        self._switch_tab("servers")
                    event.stop()
                    event.prevent_default()
                return

            # Results browsing (input not focused)
            if event.key in ("j", "down"):
                self._set_search_cursor(self.search_cursor + 1)
            elif event.key in ("k", "up"):
                if self.search_cursor == 0:
                    self.query_one("#search-input", Input).focus()
                    self._input_focused = True
                else:
                    self._set_search_cursor(self.search_cursor - 1)
            elif event.key == "enter":
                self._show_search_detail()
            elif event.key in ("left", "h"):
                self._switch_tab("servers")
            elif event.key in ("right",):
                self._switch_tab("discover")
            elif event.key == "tab":
                self.query_one("#search-input", Input).focus()
                self._input_focused = True
            elif event.key in ("q", "escape"):
                self._switch_tab("servers")
            event.stop()
            event.prevent_default()
            return

        # -- Discover tab --
        if self.active_tab == "discover":
            if event.key in ("j", "down"):
                self._set_discover_cursor(self.discover_cursor + 1)
            elif event.key in ("k", "up"):
                self._set_discover_cursor(self.discover_cursor - 1)
            elif event.key == "enter":
                self._discover_enter()
            elif event.key in ("left", "h"):
                self._switch_tab("search")
            elif event.key in ("right",):
                self._switch_tab("skills")
            elif event.key == "r":
                self._show_categories()
            elif event.key == "escape":
                self._discover_back()
            elif event.key == "q":
                self._switch_tab("servers")
            event.stop()
            event.prevent_default()
            return

        # -- Skills tab --
        if self.active_tab == "skills":
            if is_input:
                inp = self.query_one("#skills-search-input", Input)
                if event.key in ("tab", "down"):
                    if self._navigable_skill_rows():
                        inp.blur()
                        self._input_focused = False
                        self._set_skills_cursor(self.skills_cursor)
                        event.stop()
                        event.prevent_default()
                elif event.key == "left" and not inp.value:
                    self._switch_tab("discover")
                    event.stop()
                    event.prevent_default()
                elif event.key == "escape":
                    if inp.value:
                        inp.value = ""
                        # Reload default skills view
                        self.skills_loaded = False
                        self._load_skills()
                    else:
                        self._switch_tab("discover")
                    event.stop()
                    event.prevent_default()
                return

            # Results browsing (input not focused)
            if event.key in ("j", "down"):
                self._set_skills_cursor(self.skills_cursor + 1)
            elif event.key in ("k", "up"):
                if self.skills_cursor == 0:
                    self.query_one("#skills-search-input", Input).focus()
                    self._input_focused = True
                else:
                    self._set_skills_cursor(self.skills_cursor - 1)
            elif event.key == "enter":
                self._skills_enter()
            elif event.key == "d":
                rows = self._navigable_skill_rows()
                current = rows[self.skills_cursor] if rows and self.skills_cursor < len(rows) else None
                if isinstance(current, SkillRow) and current.installed:
                    self._remove_skill(current)
            elif event.key in ("left", "h"):
                self._switch_tab("discover")
            elif event.key == "tab":
                self.query_one("#skills-search-input", Input).focus()
                self._input_focused = True
            elif event.key in ("q", "escape"):
                self._switch_tab("servers")
            event.stop()
            event.prevent_default()
            return

    # -- Server tab helpers --

    def _tool_count_for(self, name: str, cfg: dict | None = None) -> int:
        if cfg is not None:
            return estimate_tool_count(name, cfg, self.tool_counts, self.cached_tool_counts)
        # For readonly rows, check cache then permission counts
        if name in self.cached_tool_counts:
            return self.cached_tool_counts[name]
        return tool_count_for(name, self.tool_counts)

    def _plugin_widgets(self):
        if not self.plugin_ids:
            return
        offset = len(self.names)
        yield Label(
            " [dim]── plugins ──[/dim]",
            classes="readonly-header",
        )
        for i, pid in enumerate(self.plugin_ids):
            display_name = pid.split("@")[0]
            tc = self._tool_count_for(f"plugin:{display_name}")
            if tc == 0:
                tc = self._tool_count_for(f"plugin_{display_name}_{display_name}")
            row = PluginRow(
                plugin_id=pid,
                enabled=self.plugins.get(pid, False),
                tool_count=tc,
            )
            row.focused = (offset + i) == self.cursor
            yield row

    def _readonly_widgets(self):
        readonly_filtered = {
            n: s for n, s in self.readonly_mcps.items()
            if n not in self.store and not n.startswith("plugin:")
        }
        if not readonly_filtered:
            return

        cats = categorize_readonly(readonly_filtered)
        cats.pop("plugin", None)
        if not cats:
            return

        cat_labels = {
            "cloud": "cloud connectors (claude.ai)",
            "user_mcp": "user MCPs (~/.claude/.mcp.json)",
        }
        cat_hints = {
            "cloud": "manage at [bold]claude.ai/settings[/bold] or [bold]/mcp[/bold] in Claude Code",
            "user_mcp": "edit directly or [bold]claude mcp remove <name> -s user[/bold]",
        }

        for cat_key, entries in cats.items():
            yield Label(
                f" [dim]── {cat_labels[cat_key]} ──[/dim]",
                classes="readonly-header",
            )
            for name, status in sorted(entries.items()):
                yield ReadOnlyRow(name, status, tool_count=self._tool_count_for(name))
            yield Label(
                f"  [dim]{cat_hints[cat_key]}[/dim]",
                classes="readonly-hint",
            )

    def _focusable_rows(self) -> list[MCPRow | PluginRow]:
        return list(self.query(MCPRow)) + list(self.query(PluginRow))

    def _rows(self) -> list[MCPRow]:
        return list(self.query(MCPRow))

    def _set_cursor(self, idx: int):
        rows = self._focusable_rows()
        if not rows:
            return
        idx = max(0, min(idx, len(rows) - 1))
        if self.cursor < len(rows):
            rows[self.cursor].focused = False
        self.cursor = idx
        rows[self.cursor].focused = True
        rows[self.cursor].scroll_visible()

    def action_move_up(self):
        if self.active_tab != "servers":
            return
        self._set_cursor(self.cursor - 1)

    def action_move_down(self):
        if self.active_tab != "servers":
            return
        self._set_cursor(self.cursor + 1)

    def _current_row(self) -> MCPRow | PluginRow | None:
        rows = self._focusable_rows()
        if rows and self.cursor < len(rows):
            return rows[self.cursor]
        return None

    def action_toggle_user(self):
        if self.active_tab != "servers":
            return
        row = self._current_row()
        if isinstance(row, MCPRow):
            row.toggle_user()
        elif isinstance(row, PluginRow):
            row.toggle_enabled()

    def action_toggle_project(self):
        if self.active_tab != "servers":
            return
        row = self._current_row()
        if isinstance(row, MCPRow):
            row.toggle_project()

    def action_toggle_enabled(self):
        if self.active_tab != "servers":
            return
        row = self._current_row()
        if isinstance(row, PluginRow):
            row.toggle_enabled()

    def action_select_or_detail(self):
        if self.active_tab == "servers":
            row = self._current_row()
            if not isinstance(row, MCPRow):
                return

            def handle_result(result):
                if result == "delete":
                    self._confirm_delete()

            self.push_screen(DetailScreen(row.mcp_name, row.mcp_cfg), handle_result)

    def action_delete(self):
        if self.active_tab != "servers":
            return
        self._confirm_delete()

    def _confirm_delete(self):
        row = self._current_row()
        if row is None:
            return

        if isinstance(row, MCPRow):
            name = row.mcp_name
            msg = f"Delete [bold]{name}[/bold] from MCP store?"
        elif isinstance(row, PluginRow):
            name = row.display_name
            msg = f"Remove plugin [bold]{name}[/bold] from settings?"
        else:
            return

        def handle_confirm(confirmed: bool):
            if confirmed:
                self._do_delete()

        self.push_screen(ConfirmScreen(msg), handle_confirm)

    def _do_delete(self):
        row = self._current_row()
        if isinstance(row, MCPRow):
            name = row.mcp_name
            del self.store[name]
            save_store(self.store, self.store_file)
            self.user_active.discard(name)
            self.project_active.discard(name)
            kwargs = {"claude_json": self.claude_json} if self.claude_json else {}
            remove_mcp_everywhere(name, self.cwd, **kwargs)
            self.names = sorted(self.store.keys())
            self._rebuild_list()
            self.query_one("#tab-bar", Label).update(self._render_tab_bar())
            self.query_one("#status-bar", Static).update(
                f"  Deleted '{name}' from store and config files"
            )
        elif isinstance(row, PluginRow):
            data = load_settings(self.settings_json)
            data.get("enabledPlugins", {}).pop(row.plugin_id, None)
            save_settings(data, self.settings_json)
            self.plugins = data.get("enabledPlugins", {})
            self.plugin_ids = sorted(self.plugins.keys())
            self._rebuild_list()
            self.query_one("#status-bar", Static).update(
                f"  Removed plugin '{row.display_name}' from settings"
            )

    def _rebuild_list(self):
        container = self.query_one("#mcp-list", ScrollableContainer)
        container.remove_children()
        for i, name in enumerate(self.names):
            cfg = self.store[name]
            new_row = MCPRow(
                name=name,
                cfg=cfg,
                in_user=(name in self.user_active),
                in_project=(name in self.project_active),
                tool_count=self._tool_count_for(name, cfg),
            )
            new_row.focused = i == self.cursor
            container.mount(new_row)
        for widget in self._plugin_widgets():
            container.mount(widget)
        for widget in self._readonly_widgets():
            container.mount(widget)
        total_focusable = len(self.names) + len(self.plugin_ids)
        self._set_cursor(min(self.cursor, total_focusable - 1))

    def action_apply(self):
        if self.active_tab != "servers":
            return
        mcp_rows = self._rows()
        user_mcps = {r.mcp_name for r in mcp_rows if r.in_user}
        project_mcps = {r.mcp_name for r in mcp_rows if r.in_project}
        kwargs = {"claude_json": self.claude_json} if self.claude_json else {}
        mcp_msg = apply_changes(self.store, user_mcps, project_mcps, self.cwd, **kwargs)
        self.user_active = user_mcps
        self.project_active = project_mcps

        plugin_rows = list(self.query(PluginRow))
        if plugin_rows:
            new_plugins = {r.plugin_id: r.enabled for r in plugin_rows}
            plugin_msg = apply_plugin_changes(new_plugins, self.settings_json)
            self.plugins = new_plugins
        else:
            plugin_msg = ""

        parts = [mcp_msg]
        if plugin_msg:
            parts.append(plugin_msg)
        self.query_one("#status-bar", Static).update("  |  ".join(parts))

    def action_quit_app(self):
        if self.active_tab != "servers":
            self._switch_tab("servers")
            return
        self.exit()
