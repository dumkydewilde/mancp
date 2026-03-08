"""Textual TUI for mancp."""

import json
import subprocess
import sys
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
    get_connector_auth_url,
    get_desktop_extensions,
    get_desktop_mcps,
    get_mcp_tool_names,
    estimate_mcp_tokens,
    estimate_tool_count,
    get_plugins,
    get_project_scope_mcps,
    get_user_scope_mcps,
    load_plugin_metadata,
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


def _clean_skills_output(raw: str) -> str:
    """Strip ANSI codes and ASCII art banner from npx skills CLI output."""
    import re
    clean = re.sub(r'\x1b\[[0-9;]*m', '', raw.strip())
    lines = [l.strip() for l in clean.splitlines()
             if l.strip() and not any(c in l for c in "═║╔╗╚╝█▀▄")]
    return lines[-1] if lines else clean[:80]


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


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        elif sys.platform == "linux":
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                check=True,
            )
        elif sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode(), check=True)
        else:
            return False
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


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

    focused: reactive[bool] = reactive(False)

    def __init__(self, name: str, status: str, tool_count: int = 0, mcp_cfg: dict | None = None):
        super().__init__()
        self.mcp_name = name
        self.status = status
        self.tool_count = tool_count
        self.mcp_cfg = mcp_cfg or {}

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        if self.status == "connected":
            indicator = "[#10b981]✓[/]"
        elif self.status == "needs auth":
            indicator = "[#d97706]⚠[/]"
        elif self.status.startswith("desktop"):
            indicator = "[#6FC2FF]◇[/]"
        elif "disabled" in self.status:
            indicator = "[#6b7280]·[/]"
        else:
            indicator = "[#6b7280]·[/]"

        badge = _format_token_badge(self.tool_count)
        padded_name = self.mcp_name.ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"
        return f"{cursor}{indicator}     {padded_name} {badge}  [dim]{self.status}[/dim]"


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
    """Single row for an installed skill: name + independent U and P toggles."""

    can_focus = False

    focused: reactive[bool] = reactive(False)
    in_user: reactive[bool] = reactive(False)
    in_project: reactive[bool] = reactive(False)
    disabled: reactive[bool] = reactive(False)

    def __init__(
        self,
        skill_name: str,
        source: str,
        in_user: bool = False,
        in_project: bool = False,
        disabled: bool = False,
        description: str = "",
        token_estimate: int = 0,
    ):
        super().__init__()
        self.skill_name = skill_name
        self.source = source
        self.in_user = in_user
        self.in_project = in_project
        self.disabled = disabled
        self.description = description
        self.token_estimate = token_estimate

    def render(self) -> str:
        cursor = "[bold]>[/] " if self.focused else "  "
        u = "[#10b981]✓[/]" if self.in_user else "[#6b7280]✗[/]"
        p = "[#10b981]✓[/]" if self.in_project else "[#6b7280]✗[/]"

        if self.token_estimate > 0:
            tk = f"{self.token_estimate // 1000}k" if self.token_estimate >= 1000 else str(self.token_estimate)
            padded = tk.rjust(BADGE_WIDTH)
            level = token_warning_level(self.token_estimate)
            if level == "high":
                badge = f"[#ef4444]{padded}[/]"
            elif level == "medium":
                badge = f"[#d97706]{padded}[/]"
            else:
                badge = f"[#6b7280]{padded}[/]"
        else:
            badge = f"[#6b7280]{'·':>{BADGE_WIDTH}}[/]"

        padded_name = self.skill_name[:NAME_WIDTH].ljust(NAME_WIDTH)
        if self.disabled:
            padded_name = f"[#4b5563]○ {padded_name}[/]"
        elif self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        meta = self.description if self.description else self.source
        return f"{cursor}{u}  {p}  {padded_name} {badge}  [dim]{meta[:46]}[/dim]"

    def toggle_user(self):
        self.in_user = not self.in_user

    def toggle_project(self):
        self.in_project = not self.in_project


class SkillSearchRow(Widget):
    """A search/discover result row for skills (not yet installed)."""

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
            badge_col = f"[dim]{inst:>6}[/]"
        else:
            badge_col = f"{'':>6}"

        meta = self.description if self.description else self.source
        return f"{cursor}{indicator}  {padded_name} {badge_col}  [dim]{meta[:46]}[/dim]"


class SkillDetailScreen(ModalScreen):
    """Detail view for an installed skill."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
    ]

    def __init__(self, skill_name: str, source: str, in_user: bool = False, in_project: bool = False, disabled: bool = False, description: str = "", token_estimate: int = 0):
        super().__init__()
        self.skill_name = skill_name
        self.source = source
        self.in_user = in_user
        self.in_project = in_project
        self.skill_disabled = disabled
        self.description = description
        self.token_estimate = token_estimate

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.source}" if "/" in self.source and self.source != "local" else ""

    def compose(self) -> ComposeResult:
        lines = [f"[bold]{self.skill_name}[/bold]"]

        meta_parts = []
        if self.source and self.source != "local":
            meta_parts.append(self.source)
        scopes = []
        if self.in_user:
            scopes.append("user")
        if self.in_project:
            scopes.append("project")
        if scopes:
            meta_parts.append(f"{' + '.join(scopes)} scope")
        if meta_parts:
            lines.append(f"[dim]{' · '.join(meta_parts)}[/dim]")

        state = "[#d97706]disabled[/]" if self.skill_disabled else "[#10b981]enabled[/]"
        if self.token_estimate > 0:
            tk = f"~{self.token_estimate:,}"
            lines.append(f"{state} [dim]· {tk} tokens when activated[/dim]")
        else:
            lines.append(state)

        if self.description:
            lines += ["", self.description]

        lines += [
            "",
            f"[dim]{'─' * 46}[/dim]",
        ]
        toggle_label = "enable" if self.skill_disabled else "disable"
        actions = f"[bold]E[/bold] {toggle_label} · [bold]D[/bold] remove"
        if self.github_url:
            actions += " · [bold]o[/bold] open in browser"
        actions += " · [bold]c[/bold] copy"
        lines.append(actions)
        lines.append("[bold]Q / Esc[/bold] close")

        with ScrollableContainer(id="skill-detail-box"):
            yield Label("\n".join(lines), id="skill-detail-content")
            if not self.description:
                yield Static("[dim]Loading description...[/dim]", id="skill-readme")

    def on_mount(self):
        if not self.description and "/" in self.source:
            thread = Thread(target=self._fetch_description, daemon=True)
            thread.start()

    def _fetch_description(self):
        desc = fetch_skill_description(self.source, self.skill_name)
        try:
            self.app.call_from_thread(self._show_description, desc)
        except Exception:
            pass

    def _show_description(self, desc: str):
        try:
            widget = self.query_one("#skill-readme", Static)
        except Exception:
            return
        if desc:
            widget.update(f"\n{desc}")
        else:
            widget.update("")

    def action_close(self):
        self.dismiss(None)

    def action_delete(self):
        self.dismiss("delete")

    def action_toggle_enabled(self):
        self.dismiss("toggle_enabled")

    def action_open_link(self):
        if self.github_url:
            subprocess.Popen(["open", self.github_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def key_o(self):
        self.action_open_link()

    def key_d(self):
        self.action_delete()

    def key_e(self):
        self.action_toggle_enabled()

    def key_c(self):
        """Copy skill install command to clipboard."""
        text = f"claude skill install {self.source}"
        if _copy_to_clipboard(text):
            try:
                widget = self.query_one("#skill-detail-content", Label)
                widget.update(widget.renderable + "\n[#10b981]Copied to clipboard![/]")
            except Exception:
                pass

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


class SkillSearchDetailScreen(ModalScreen):
    """Detail view for a skill search result with option to install."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
    ]

    def __init__(self, skill_name: str, source: str, installs: int = 0, installed: bool = False, description: str = ""):
        super().__init__()
        self.skill_name = skill_name
        self.source = source
        self.installs = installs
        self.is_installed = installed
        self.description = description

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.source}" if "/" in self.source and self.source != "local" else ""

    def compose(self) -> ComposeResult:
        lines = [f"[bold]{self.skill_name}[/bold]"]

        meta_parts = []
        if self.source and self.source != "local":
            meta_parts.append(self.source)
        if self.installs:
            meta_parts.append(f"{self.installs:,} installs")
        if meta_parts:
            lines.append(f"[dim]{' · '.join(meta_parts)}[/dim]")

        if self.is_installed:
            lines.append("[#10b981]already installed[/]")

        if self.description:
            lines += ["", self.description]

        lines += [
            "",
            f"[dim]{'─' * 46}[/dim]",
        ]
        if not self.is_installed:
            actions = "[bold]U[/bold] install user · [bold]P[/bold] install project"
        else:
            actions = "[dim]already installed[/dim]"
        if self.github_url:
            actions += " · [bold]o[/bold] open in browser"
        actions += " · [bold]c[/bold] copy"
        lines.append(actions)
        lines.append("[bold]Q / Esc[/bold] close")

        with ScrollableContainer(id="skill-search-detail-box"):
            yield Label("\n".join(lines), id="skill-search-detail-content")
            if not self.description:
                yield Static("[dim]Loading description...[/dim]", id="skill-search-readme")

    def on_mount(self):
        if not self.description and "/" in self.source:
            thread = Thread(target=self._fetch_description, daemon=True)
            thread.start()

    def _fetch_description(self):
        desc = fetch_skill_description(self.source, self.skill_name)
        try:
            self.app.call_from_thread(self._show_description, desc)
        except Exception:
            pass

    def _show_description(self, desc: str):
        try:
            widget = self.query_one("#skill-search-readme", Static)
        except Exception:
            return
        if desc:
            widget.update(f"\n{desc}")
        else:
            widget.update("")

    def action_close(self):
        self.dismiss(None)

    def key_u(self):
        if not self.is_installed:
            self.dismiss("add_user")

    def key_p(self):
        if not self.is_installed:
            self.dismiss("add_project")

    def key_o(self):
        if self.github_url:
            subprocess.Popen(["open", self.github_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def key_c(self):
        text = f"claude skill install {self.source}"
        if _copy_to_clipboard(text):
            try:
                widget = self.query_one("#skill-search-detail-content", Label)
                widget.update(widget.renderable + "\n[#10b981]Copied to clipboard![/]")
            except Exception:
                pass

    DEFAULT_CSS = """
    SkillSearchDetailScreen {
        align: center middle;
    }
    #skill-search-detail-box {
        width: 72;
        max-height: 85%;
        border: solid $accent;
        padding: 1 2;
    }
    #skill-search-detail-content {
        width: 100%;
        height: auto;
    }
    #skill-search-readme {
        width: 100%;
        height: auto;
    }
    """


class SkillsDiscoverScreen(ModalScreen):
    """Full-screen modal for browsing discoverable skills."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
    ]

    def __init__(self, installed_names: set[str]):
        super().__init__()
        self.installed_names = installed_names
        self._skills: list[Skill] = []
        self._cursor = 0

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="skills-discover-box"):
            yield Static("[dim]Loading skills from skills.sh...[/dim]", id="skills-discover-status")
            yield ScrollableContainer(id="skills-discover-list")

    def on_mount(self):
        thread = Thread(target=self._fetch_skills, daemon=True)
        thread.start()

    def _fetch_skills(self):
        skills = fetch_skills_discover(count=75)
        self.app.call_from_thread(self._show_skills, skills)

    def _show_skills(self, skills: list[Skill]):
        self._skills = [s for s in skills if s.name not in self.installed_names]
        container = self.query_one("#skills-discover-list", ScrollableContainer)
        container.remove_children()
        for s in self._skills:
            row = SkillSearchRow(
                skill_name=s.name,
                source=s.source,
                installs=s.installs,
                installed=s.name in self.installed_names,
            )
            container.mount(row)
        self.query_one("#skills-discover-status", Static).update(
            f"[bold]{len(self._skills)} skills[/bold] [dim]· Enter to view · U user install · P project install · Q to close[/dim]"
        )
        self._set_cursor(0)

    def _navigable_rows(self) -> list[SkillSearchRow]:
        return list(self.query(SkillSearchRow))

    def _set_cursor(self, idx: int):
        rows = self._navigable_rows()
        if not rows:
            return
        idx = max(0, min(idx, len(rows) - 1))
        for r in rows:
            r.focused = False
        self._cursor = idx
        rows[idx].focused = True
        rows[idx].scroll_visible()

    def on_key(self, event):
        if event.key in ("j", "down"):
            self._set_cursor(self._cursor + 1)
            event.stop()
        elif event.key in ("k", "up"):
            self._set_cursor(self._cursor - 1)
            event.stop()
        elif event.key == "enter":
            rows = self._navigable_rows()
            if rows and self._cursor < len(rows):
                row = rows[self._cursor]
                self.app.push_screen(SkillSearchDetailScreen(
                    skill_name=row.skill_name,
                    source=row.source,
                    installs=row.installs,
                    installed=row.installed,
                ), self._handle_detail_result)
            event.stop()
        elif event.key == "u":
            rows = self._navigable_rows()
            if rows and self._cursor < len(rows):
                row = rows[self._cursor]
                if not row.installed:
                    self.dismiss(("install", row.skill_name, row.source, "global"))
            event.stop()
        elif event.key == "p":
            rows = self._navigable_rows()
            if rows and self._cursor < len(rows):
                row = rows[self._cursor]
                if not row.installed:
                    self.dismiss(("install", row.skill_name, row.source, "project"))
            event.stop()
        elif event.key in ("q", "escape"):
            self.dismiss(None)
            event.stop()

    def _handle_detail_result(self, result):
        if result in ("add_user", "add_project"):
            scope = "global" if result == "add_user" else "project"
            rows = self._navigable_rows()
            if rows and self._cursor < len(rows):
                row = rows[self._cursor]
                self.dismiss(("install", row.skill_name, row.source, scope))

    def action_close(self):
        self.dismiss(None)

    DEFAULT_CSS = """
    SkillsDiscoverScreen {
        align: center middle;
    }
    #skills-discover-box {
        width: 90%;
        max-width: 100;
        height: 85%;
        border: solid $accent;
        padding: 1 2;
    }
    #skills-discover-status {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }
    #skills-discover-list {
        width: 100%;
        height: 1fr;
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
        Binding("c", "copy", "Copy config"),
    ]

    def __init__(self, name: str, cfg: dict, permitted_tools: list[str] | None = None, readonly: bool = False, importable: bool = False, needs_auth: bool = False, claude_dir: Path = CLAUDE_DIR, claude_json: Path | None = None):
        super().__init__()
        self.mcp_name = name
        self.mcp_cfg = cfg
        self.permitted_tools = set(permitted_tools or [])
        self.readonly = readonly
        self.importable = importable
        self.needs_auth = needs_auth
        self.claude_dir = claude_dir
        self.claude_json = claude_json

    def compose(self) -> ComposeResult:
        masked = mask_secrets(self.mcp_cfg)
        pretty = json.dumps(masked, indent=2)

        lines = [
            f"[bold]{self.mcp_name}[/bold]",
        ]
        if self.needs_auth:
            lines.append("[#d97706]needs authentication[/]")
        lines += [
            "",
            "[dim]Configuration (secrets masked):[/dim]",
            "",
        ]
        for line in pretty.splitlines():
            lines.append(f"  {line}")

        with Vertical(id="detail-box"):
            with ScrollableContainer(id="detail-scroll"):
                yield Label("\n".join(lines), id="detail-content")
                yield Static("[dim]Loading tools...[/dim]", id="detail-tools")
            # Pinned action bar at bottom
            actions = []
            if self.needs_auth:
                actions.append("[bold]A[/bold] authenticate")
            if self.importable:
                actions.append("[bold]I[/bold] import to store")
            if not self.readonly:
                actions.append("[bold]D[/bold] delete")
            actions.append("[bold]C[/bold] copy config")
            actions.append("[bold]Q / Esc[/bold] close")
            yield Static("  " + "   ".join(actions), id="detail-actions")

    def on_mount(self):
        thread = Thread(target=self._fetch_tools, daemon=True)
        thread.start()

    def _fetch_tools(self):
        from .registry import inspect_mcp_tools
        tools = inspect_mcp_tools(self.mcp_cfg)
        try:
            self.app.call_from_thread(self._show_tools, tools)
        except Exception:
            pass

    def _show_tools(self, tools: list[dict] | None):
        try:
            widget = self.query_one("#detail-tools", Static)
        except Exception:
            return

        if tools is None:
            # Fallback: show permitted tools from settings.local.json
            if self.permitted_tools:
                lines = [f"[dim]Permitted tools ({len(self.permitted_tools)}):[/dim]", ""]
                for name in sorted(self.permitted_tools):
                    lines.append(f"  [#10b981]✓[/] {name}")
                lines.insert(0, "[dim]Could not connect · showing permitted tools from settings[/dim]")
                widget.update("\n".join(lines))
            else:
                widget.update("[dim]Could not connect to server[/dim]")
            return
        if not tools:
            widget.update("[dim]No tools exposed[/dim]")
            return

        lines = [f"[dim]Tools ({len(tools)}):[/dim]", ""]
        for t in sorted(tools, key=lambda x: x.get("name", "")):
            name = t.get("name", "")
            desc = t.get("description", "")
            if name in self.permitted_tools:
                indicator = "[#10b981]✓[/]"
            else:
                indicator = "[#6b7280]·[/]"
            line = f"[bold white]{name}[/]"
            if desc:
                short_desc = desc[:120] + "…" if len(desc) > 120 else desc
                line += f"  [dim]{short_desc}[/dim]"
            lines.append(line)

        if self.permitted_tools:
            active = sum(1 for t in tools if t.get("name", "") in self.permitted_tools)
            lines.insert(0, f"[dim]{active}/{len(tools)} permitted[/dim]")

        widget.update("\n".join(lines))

    def action_close(self):
        self.dismiss(None)

    def action_delete(self):
        if not self.readonly:
            self.dismiss("delete")

    def action_copy(self):
        """Copy the MCP config JSON to clipboard."""
        cfg_json = json.dumps({self.mcp_name: self.mcp_cfg}, indent=2)
        if _copy_to_clipboard(cfg_json):
            try:
                widget = self.query_one("#detail-tools", Static)
                widget.update(widget.renderable + "\n[#10b981]Copied to clipboard![/]")
            except Exception:
                pass

    def action_import(self):
        if self.importable:
            self.dismiss("import")

    def action_authenticate(self):
        if not self.needs_auth:
            return
        import webbrowser
        claude_json = self.claude_json or Path.home() / ".claude.json"
        url = get_connector_auth_url(self.mcp_name, self.claude_dir, claude_json)
        if url:
            webbrowser.open(url)
            self.dismiss("auth_started")
        else:
            try:
                widget = self.query_one("#detail-tools", Static)
                widget.update("[#ef4444]Could not build auth URL — no server ID found in debug logs[/]")
            except Exception:
                pass

    def key_a(self):
        self.action_authenticate()

    def key_i(self):
        self.action_import()

    DEFAULT_CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail-box {
        width: 80;
        height: 85%;
        border: solid $accent;
        padding: 1 2;
    }
    #detail-scroll {
        width: 100%;
        height: 1fr;
    }
    #detail-content {
        width: 100%;
        height: auto;
    }
    #detail-tools {
        width: 100%;
        height: auto;
        margin-top: 1;
    }
    #detail-actions {
        width: 100%;
        height: 2;
        dock: bottom;
        border-top: solid #444444;
    }
    """


class PluginDetailScreen(ModalScreen):
    """Detail view for a plugin with metadata from installed_plugins.json."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("e", "toggle", "Toggle enabled"),
        Binding("d", "delete", "Delete"),
    ]

    def __init__(self, plugin_id: str, enabled: bool, claude_dir: Path, permitted_tools: list[str] | None = None):
        super().__init__()
        self.plugin_id = plugin_id
        self.display_name = plugin_id.split("@")[0]
        self.marketplace = plugin_id.split("@")[1] if "@" in plugin_id else ""
        self.plugin_enabled = enabled
        self.claude_dir = claude_dir
        self.permitted_tools = set(permitted_tools or [])

    def compose(self) -> ComposeResult:
        meta = load_plugin_metadata(self.plugin_id, self.claude_dir)

        lines = [f"[bold]{self.display_name}[/bold]"]

        # Status line
        state = "[#10b981]enabled[/]" if self.plugin_enabled else "[#6b7280]disabled[/]"
        lines.append(state)

        # Description
        desc = meta.get("description", "")
        if desc:
            lines += ["", desc]

        # Metadata
        lines.append("")
        if meta.get("author"):
            lines.append(f"[dim]Author:[/dim]       {meta['author']}")
        if self.marketplace:
            lines.append(f"[dim]Marketplace:[/dim]   {self.marketplace}")
        if meta.get("version"):
            lines.append(f"[dim]Version:[/dim]      {meta['version']}")
        if meta.get("category"):
            lines.append(f"[dim]Category:[/dim]     {meta['category']}")
        if meta.get("homepage"):
            lines.append(f"[dim]Homepage:[/dim]     {meta['homepage']}")
        elif meta.get("source_url"):
            lines.append(f"[dim]Source:[/dim]       {meta['source_url']}")
        if meta.get("installed_at"):
            lines.append(f"[dim]Installed:[/dim]    {meta['installed_at'][:10]}")
        if meta.get("last_updated"):
            lines.append(f"[dim]Updated:[/dim]      {meta['last_updated'][:10]}")
        if meta.get("keywords"):
            lines.append(f"[dim]Keywords:[/dim]     {', '.join(meta['keywords'])}")

        # MCP config
        mcp_cfg = meta.get("mcp_config", {})
        if mcp_cfg:
            lines += ["", "[dim]MCP configuration:[/dim]", ""]
            masked = mask_secrets(mcp_cfg)
            for line in json.dumps(masked, indent=2).splitlines():
                lines.append(f"  {line}")

        with Vertical(id="plugin-detail-box"):
            with ScrollableContainer(id="plugin-detail-scroll"):
                yield Label("\n".join(lines), id="plugin-detail-content")
                yield Static("[dim]Loading tools...[/dim]", id="plugin-detail-tools")
            # Pinned actions
            toggle_label = "disable" if self.plugin_enabled else "enable"
            yield Static(
                f"  [bold]E[/bold] {toggle_label}   [bold]D[/bold] delete   [bold]C[/bold] copy config   [bold]Q / Esc[/bold] close",
                id="plugin-detail-actions",
            )

    def on_mount(self):
        if self.permitted_tools:
            self._show_permitted_tools()
        else:
            # Try to fetch tools via MCP protocol
            meta = load_plugin_metadata(self.plugin_id, self.claude_dir)
            mcp_cfg = meta.get("mcp_config", {})
            if mcp_cfg:
                # MCP config has server names as keys, pick the first
                first_cfg = next(iter(mcp_cfg.values()), {})
                if first_cfg:
                    thread = Thread(target=self._fetch_tools, args=(first_cfg,), daemon=True)
                    thread.start()
                    return
            try:
                self.query_one("#plugin-detail-tools", Static).update("")
            except Exception:
                pass

    def _fetch_tools(self, cfg: dict):
        from .registry import inspect_mcp_tools
        tools = inspect_mcp_tools(cfg)
        try:
            self.app.call_from_thread(self._show_tools, tools)
        except Exception:
            pass

    def _show_tools(self, tools: list[dict] | None):
        try:
            widget = self.query_one("#plugin-detail-tools", Static)
        except Exception:
            return
        if tools is None:
            if self.permitted_tools:
                self._show_permitted_tools()
            else:
                widget.update("[dim]Could not connect to server[/dim]")
            return
        if not tools:
            widget.update("[dim]No tools exposed[/dim]")
            return

        lines = [f"[dim]Tools ({len(tools)}):[/dim]", ""]
        for t in sorted(tools, key=lambda x: x.get("name", "")):
            name = t.get("name", "")
            desc = t.get("description", "")
            indicator = "[#10b981]✓[/]" if name in self.permitted_tools else "[#6b7280]·[/]"
            line = f"[bold white]{name}[/]"
            if desc:
                short_desc = desc[:120] + "…" if len(desc) > 120 else desc
                line += f"  [dim]{short_desc}[/dim]"
            lines.append(line)

        if self.permitted_tools:
            active = sum(1 for t in tools if t.get("name", "") in self.permitted_tools)
            lines.insert(0, f"[dim]{active}/{len(tools)} permitted[/dim]")

        widget.update("\n".join(lines))

    def _show_permitted_tools(self):
        try:
            widget = self.query_one("#plugin-detail-tools", Static)
        except Exception:
            return
        lines = [f"[dim]Permitted tools ({len(self.permitted_tools)}):[/dim]", ""]
        for name in sorted(self.permitted_tools):
            lines.append(f"  [#10b981]✓[/] {name}")
        widget.update("\n".join(lines))

    def action_close(self):
        self.dismiss(None)

    def action_toggle(self):
        self.dismiss("toggle")

    def action_delete(self):
        self.dismiss("delete")

    def key_c(self):
        """Copy plugin MCP config to clipboard."""
        meta = load_plugin_metadata(self.plugin_id, self.claude_dir)
        mcp_cfg = meta.get("mcp_config", {})
        if mcp_cfg:
            cfg_json = json.dumps(mcp_cfg, indent=2)
        else:
            cfg_json = self.plugin_id
        if _copy_to_clipboard(cfg_json):
            try:
                widget = self.query_one("#plugin-detail-tools", Static)
                widget.update(widget.renderable + "\n[#10b981]Copied to clipboard![/]")
            except Exception:
                pass

    DEFAULT_CSS = """
    PluginDetailScreen {
        align: center middle;
    }
    #plugin-detail-box {
        width: 80;
        height: 85%;
        border: solid $accent;
        padding: 1 2;
    }
    #plugin-detail-scroll {
        width: 100%;
        height: 1fr;
    }
    #plugin-detail-content {
        width: 100%;
        height: auto;
    }
    #plugin-detail-tools {
        width: 100%;
        height: auto;
        margin-top: 1;
    }
    #plugin-detail-actions {
        width: 100%;
        height: 2;
        dock: bottom;
        border-top: solid #444444;
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
        lines.append("  [bold]C[/bold]  copy config")
        lines.append("  [bold]Q / Esc[/bold]  close")

        with ScrollableContainer(id="search-detail-box"):
            yield Label("\n".join(lines), id="search-detail-content")

    def action_close(self):
        self.dismiss(None)

    def action_add(self):
        if not self.already_in_store:
            self.dismiss("add")

    def key_c(self):
        """Copy the MCP config JSON to clipboard."""
        cfg = self.result.to_mcp_config()
        name = normalize_server_name(self.result.name)
        cfg_json = json.dumps({name: cfg}, indent=2)
        if _copy_to_clipboard(cfg_json):
            try:
                widget = self.query_one("#search-detail-content", Label)
                widget.update(widget.renderable + "\n[#10b981]Copied config to clipboard![/]")
            except Exception:
                pass

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
        height: auto;
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
            "  [bold]C[/bold]  copy server name",
            "  [bold]Q / Esc[/bold]  close",
        ]

        with ScrollableContainer(id="discover-detail-box"):
            yield Label("\n".join(lines), id="discover-detail-content")

    def action_close(self):
        self.dismiss(None)

    def action_add(self):
        self.dismiss("add")

    def key_c(self):
        """Copy server info to clipboard."""
        s = self.server
        parts = [s.name]
        if s.url:
            parts.append(s.url)
        if _copy_to_clipboard("\n".join(parts)):
            try:
                widget = self.query_one("#discover-detail-content", Label)
                widget.update(widget.renderable + "\n[#10b981]Copied to clipboard![/]")
            except Exception:
                pass

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
        height: auto;
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
        border: tall #FFDE02;
    }
    #search-input:focus {
        border: tall #FFDE02;
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
    SkillSearchRow {
        height: 1;
    }
    #skills-panel {
        height: 1fr;
        padding: 0 1;
    }
    #skills-col-header {
        height: 1;
        padding: 0 1;
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
    #search-sub-nav {
        height: 1;
        padding: 0 1;
    }
    #discover-sub-nav {
        height: 1;
        padding: 0 1;
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
        dock: bottom;
    }
    #footer-hints {
        height: 2;
        padding: 0 1;
        dock: bottom;
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
        Binding("i", "import_desktop", "Import from Desktop", show=False),
        Binding("s", "apply", "Save", show=False),
        Binding("ctrl+s", "apply", "Save", show=False),
        Binding("q,escape", "quit_app", "Quit", show=False),
    ]

    TABS = ["servers", "skills", "search", "discover"]

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
        self.desktop_mcp_configs = get_desktop_mcps()
        self.desktop_extensions = {e["display_name"]: e for e in get_desktop_extensions()}
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
        self.search_mode = "mcps"  # "mcps" or "skills"
        self.discover_mode = "mcps"  # "mcps" or "skills"

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

        # -- Skills tab (hidden initially) --
        with Vertical(id="skills-panel", classes="hidden"):
            yield Label(
                "  [bold]U[/]  [bold]P[/]  name                        [bold]ctx[/]  description",
                id="skills-col-header",
            )
            yield Static("  [dim]Loading skills...[/dim]", id="skills-status")
            yield ScrollableContainer(id="skills-results", can_focus=False)

        # -- Search tab (hidden initially) --
        with Vertical(id="search-panel", classes="hidden"):
            yield Label(self._render_search_sub_nav(), id="search-sub-nav")
            yield Input(placeholder="Search MCP servers (GitHub MCP org + Registry)...", id="search-input", disabled=True)
            yield Label(
                "  [bold]✓[/]  name                       [bold]type[/]   registry / description",
                id="search-col-header",
            )
            yield Static("  [dim]Type a query and press Enter to search[/dim]", id="search-status")
            yield ScrollableContainer(id="search-results", can_focus=False)

        # -- Discover tab (hidden initially) --
        with Vertical(id="discover-panel", classes="hidden"):
            yield Label(self._render_discover_sub_nav(), id="discover-sub-nav")
            yield Static("  [bold]Discover MCP servers[/bold]", id="discover-status")
            yield Label(
                f"     {'name':<{NAME_WIDTH}}     [bold]★[/]  [bold]lang[/]  description",
                id="discover-col-header",
            )
            yield ScrollableContainer(id="discover-results", can_focus=False)

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

    def _render_search_sub_nav(self) -> str:
        if self.search_mode == "mcps":
            return "  [bold #FFDE02] MCPs [/]  [dim] Skills [/]  [dim]· tab to switch[/dim]"
        return "  [dim] MCPs [/]  [bold #FFDE02] Skills [/]  [dim]· tab to switch[/dim]"

    def _render_discover_sub_nav(self) -> str:
        if self.discover_mode == "mcps":
            return "  [bold #FFDE02] MCPs [/]  [dim] Skills [/]  [dim]· tab to switch[/dim]"
        return "  [dim] MCPs [/]  [bold #FFDE02] Skills [/]  [dim]· tab to switch[/dim]"

    def _footer_for_tab(self) -> str:
        if self.active_tab == "servers":
            return (
                "  [dim]j/k[/dim] navigate   [dim]u[/dim] user   [dim]p[/dim] project   "
                "[dim]e[/dim] enable   [dim]enter[/dim] detail   [dim]d[/dim] delete   "
                "[dim]s[/dim] save   [dim]→[/dim] skills   [dim]q[/dim] quit"
            )
        if self.active_tab == "skills":
            return (
                "  [dim]j/k[/dim] navigate   [dim]u[/dim] user   [dim]p[/dim] project   "
                "[dim]e[/dim] enable   [dim]enter[/dim] detail   [dim]d[/dim] remove   "
                "[dim]s[/dim] save   [dim]←[/dim] servers   [dim]→[/dim] search   [dim]q[/dim] quit"
            )
        if self.active_tab == "search":
            return (
                "  [dim]type[/dim] to search   [dim]tab[/dim] mcps/skills   "
                "[dim]j/k[/dim] navigate   [dim]enter[/dim] detail   "
                "[dim]←[/dim] skills   [dim]→[/dim] discover   [dim]q[/dim] quit"
            )
        # discover
        if self.discover_mode == "mcps" and self.discover_view == "categories":
            return (
                "  [dim]j/k[/dim] navigate   [dim]enter[/dim] select   "
                "[dim]tab[/dim] mcps/skills   "
                "[dim]r[/dim] refresh   [dim]←[/dim] search   [dim]q[/dim] quit"
            )
        if self.discover_mode == "skills":
            return (
                "  [dim]j/k[/dim] navigate   [dim]enter[/dim] detail   "
                "[dim]u[/dim] user   [dim]p[/dim] project   "
                "[dim]tab[/dim] mcps/skills   "
                "[dim]←[/dim] search   [dim]q[/dim] quit"
            )
        return (
            "  [dim]j/k[/dim] navigate   [dim]enter[/dim] detail   "
            "[dim]tab[/dim] mcps/skills   "
            "[dim]esc[/dim] back   [dim]←[/dim] search   [dim]q[/dim] quit"
        )

    def _switch_tab(self, tab: str):
        if tab == self.active_tab:
            return
        self.active_tab = tab

        mcp_list = self.query_one("#mcp-list", ScrollableContainer)
        col_header = self.query_one("#col-header", Label)
        skills_panel = self.query_one("#skills-panel", Vertical)
        search_panel = self.query_one("#search-panel", Vertical)
        discover_panel = self.query_one("#discover-panel", Vertical)
        search_input = self.query_one("#search-input", Input)

        # Hide all
        mcp_list.add_class("hidden")
        col_header.add_class("hidden")
        skills_panel.add_class("hidden")
        search_panel.add_class("hidden")
        discover_panel.add_class("hidden")
        if search_input.has_focus:
            search_input.blur()
        search_input.disabled = True
        self._input_focused = False

        if tab == "servers":
            mcp_list.remove_class("hidden")
            col_header.remove_class("hidden")
        elif tab == "skills":
            skills_panel.remove_class("hidden")
            if not self.skills_loaded:
                self._load_skills()
        elif tab == "search":
            search_panel.remove_class("hidden")
            search_input.disabled = False
            search_input.focus()
            self._input_focused = True
            self._update_search_placeholder()
        elif tab == "discover":
            discover_panel.remove_class("hidden")
            if self.discover_mode == "mcps":
                if not self.query(CategoryRow) and not self.query(DiscoverRow):
                    self._show_categories()
            else:
                self._show_skills_discover()

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

    def _navigable_discover_rows(self) -> list[DiscoverRow | CategoryRow | MoreRow | SkillSearchRow]:
        """All navigable rows in discover tab, in DOM order."""
        container = self.query_one("#discover-results", ScrollableContainer)
        return [
            w for w in container.children
            if isinstance(w, (DiscoverRow, CategoryRow, MoreRow, SkillSearchRow))
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
        """Go back from category server list to categories, or to search tab."""
        if self.discover_mode == "mcps" and self.discover_view == "servers":
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

    def _import_desktop_mcp(self, name: str, cfg: dict):
        """Import a Claude Desktop MCP config into the mancp store."""
        if name in self.store:
            self.query_one("#status-bar", Static).update(f"  '{name}' already in store")
            return
        self.store[name] = cfg
        save_store(self.store, self.store_file)
        self.names = sorted(self.store.keys())
        self._rebuild_list()
        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        self.query_one("#status-bar", Static).update(
            f"  + {name} imported from Claude Desktop"
        )

    # -- Skills tab --

    def _load_skills(self):
        """Load installed skills in background."""
        self.skills_loaded = True
        self.query_one("#skills-status", Static).update("  [dim]Loading skills...[/dim]")
        thread = Thread(target=self._do_load_skills, daemon=True)
        thread.start()

    def _do_load_skills(self):
        installed = get_installed_skills(cwd=self.cwd)
        self.call_from_thread(self._show_skills, installed)

    def _show_skills(self, installed: list[dict]):
        from .registry import estimate_skills_menu_tokens, estimate_skill_tokens
        installed_names = {s["name"] for s in installed}
        self._skills_installed_names = installed_names
        container = self.query_one("#skills-results", ScrollableContainer)
        container.remove_children()
        self.skills_cursor = 0

        self._all_installed_skills = installed
        enabled_count = sum(1 for s in installed if not s.get("disabled", False))

        for s in installed:
            tokens = estimate_skill_tokens(s.get("skill_md_size", 0))
            row = SkillRow(
                skill_name=s["name"],
                source=s.get("source_repo", "") or "local",
                in_user=s.get("in_user", False),
                in_project=s.get("in_project", False),
                disabled=s.get("disabled", False),
                description=s.get("description", ""),
                token_estimate=tokens,
            )
            container.mount(row)

        menu_tokens = estimate_skills_menu_tokens(installed)
        mtk = f"{menu_tokens // 1000}k" if menu_tokens >= 1000 else str(menu_tokens)
        self.query_one("#skills-status", Static).update(
            f"  [bold]Skills[/bold]  [dim]{len(installed)} installed · {enabled_count} enabled · menu ~{mtk} tokens/conversation[/dim]"
        )
        self._set_skills_cursor(0)

    def _navigable_skill_rows(self) -> list[SkillRow]:
        container = self.query_one("#skills-results", ScrollableContainer)
        return [
            w for w in container.children
            if isinstance(w, SkillRow)
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
        self._show_skill_detail()

    def _show_skill_detail(self):
        rows = self._navigable_skill_rows()
        if not rows or self.skills_cursor >= len(rows):
            return
        row = rows[self.skills_cursor]

        def handle_result(result):
            if result == "delete":
                self._remove_skill(row)
            elif result == "toggle_enabled":
                self._toggle_skill_enabled(row)

        self.push_screen(SkillDetailScreen(
            skill_name=row.skill_name,
            source=row.source,
            in_user=row.in_user,
            in_project=row.in_project,
            disabled=row.disabled,
            description=row.description,
            token_estimate=row.token_estimate,
        ), handle_result)

    def _install_skill(self, skill_name: str, source: str, scope: str = "global"):
        """Install a skill via npx skills add.

        scope: "global" (default, installs to ~/.claude/skills) or "project"
        (installs to <cwd>/.claude/skills via .agents/skills).
        """
        scope_label = "user" if scope == "global" else "project"
        self._notify(f"  Installing {skill_name} ({scope_label})...")

        def do_install():
            try:
                cmd = [
                    "npx", "skills", "add", source,
                    "--yes", "--agent", "claude-code",
                    "--skill", skill_name,
                ]
                if scope == "global":
                    cmd.append("--global")
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=60,
                    cwd=str(self.cwd) if scope == "project" else None,
                )
                success = result.returncode == 0
                msg = _clean_skills_output(result.stdout or result.stderr)
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                success = False
                msg = str(e)
            self.call_from_thread(self._on_skill_installed, skill_name, success, msg, scope)

        thread = Thread(target=do_install, daemon=True)
        thread.start()

    def _on_skill_installed(self, skill_name: str, success: bool, msg: str, scope: str = "global"):
        if success:
            scope_label = "user" if scope == "global" else "project"
            self.skills_loaded = False
            self._load_skills()
            self._notify(f"  [#10b981]✓[/] Installed {skill_name} ({scope_label})")
        else:
            self._notify(f"  [#ef4444]✗[/] Failed: {msg[:60]}")

    def _notify(self, message: str):
        """Show a notification in the central status bar (bottom)."""
        self.query_one("#status-bar", Static).update(message)

    def _remove_skill(self, row: SkillRow):
        """Remove an installed skill via npx skills remove.

        Removes from all scopes where it's installed.
        """
        scopes_to_remove = []
        if row.in_user:
            scopes_to_remove.append("global")
        if row.in_project:
            scopes_to_remove.append("project")
        if not scopes_to_remove:
            scopes_to_remove = ["global"]

        def do_remove():
            success = True
            msg = ""
            for scope in scopes_to_remove:
                try:
                    cmd = [
                        "npx", "skills", "remove",
                        "--yes", "--agent", "claude-code",
                        "--skill", row.skill_name,
                    ]
                    if scope == "global":
                        cmd.append("--global")
                    result = subprocess.run(
                        cmd,
                        capture_output=True, text=True, timeout=30,
                        cwd=str(self.cwd) if scope == "project" else None,
                    )
                    if result.returncode != 0:
                        success = False
                        msg = _clean_skills_output(result.stdout or result.stderr)
                except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                    success = False
                    msg = str(e)
            self.call_from_thread(self._on_skill_removed, row, success, msg)

        thread = Thread(target=do_remove, daemon=True)
        thread.start()

    def _on_skill_removed(self, row: SkillRow, success: bool, msg: str):
        if success:
            self.skills_loaded = False
            self._load_skills()
            self._notify(f"  Removed {row.skill_name}")
        else:
            self._notify(f"  [#ef4444]✗[/] Failed: {msg[:60]}")

    def _remove_skill_scope(self, row: SkillRow, scope: str):
        """Remove a skill from a single scope."""
        scope_label = "user" if scope == "global" else "project"
        self._notify(f"  Removing {row.skill_name} from {scope_label}...")

        def do_remove():
            try:
                cmd = [
                    "npx", "skills", "remove",
                    "--yes", "--agent", "claude-code",
                    "--skill", row.skill_name,
                ]
                if scope == "global":
                    cmd.append("--global")
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=30,
                    cwd=str(self.cwd) if scope == "project" else None,
                )
                success = result.returncode == 0
                msg = _clean_skills_output(result.stdout or result.stderr)
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                success = False
                msg = str(e)
            self.call_from_thread(self._on_skill_scope_removed, row, scope, success, msg)

        thread = Thread(target=do_remove, daemon=True)
        thread.start()

    def _on_skill_scope_removed(self, row: SkillRow, scope: str, success: bool, msg: str):
        if success:
            self.skills_loaded = False
            scope_label = "user" if scope == "global" else "project"
            self._load_skills()
            self._notify(f"  Removed {row.skill_name} from {scope_label}")
        else:
            self._notify(f"  [#ef4444]✗[/] Failed: {msg[:60]}")

    def _toggle_skill_enabled(self, row: SkillRow):
        """Toggle a skill's enabled/disabled state in-place."""
        from .store import disable_skill, enable_skill
        # Toggle in all scopes where the skill is installed
        success = False
        if row.in_project:
            if row.disabled:
                success = enable_skill(row.skill_name, cwd=self.cwd, scope="project")
            else:
                success = disable_skill(row.skill_name, cwd=self.cwd, scope="project")
        if row.in_user:
            if row.disabled:
                success = enable_skill(row.skill_name, scope="global") or success
            else:
                success = disable_skill(row.skill_name, scope="global") or success

        action = "Enabled" if row.disabled else "Disabled"

        if success:
            row.disabled = not row.disabled
            row.refresh()
            for s in getattr(self, "_all_installed_skills", []):
                if s["name"] == row.skill_name:
                    s["disabled"] = row.disabled
                    break
            installed = getattr(self, "_all_installed_skills", [])
            enabled_count = sum(1 for s in installed if not s.get("disabled", False))
            from .registry import estimate_skills_menu_tokens
            menu_tokens = estimate_skills_menu_tokens(installed)
            mtk = f"{menu_tokens // 1000}k" if menu_tokens >= 1000 else str(menu_tokens)
            self._notify(
                f"  {action} {row.skill_name}  [dim]·  {enabled_count} enabled  ·  menu ~{mtk} tokens/conversation[/dim]"
            )
        else:
            self._notify(f"  [#ef4444]✗[/] Failed to toggle {row.skill_name}")

    # -- Search input handlers --

    def on_input_submitted(self, event: Input.Submitted):
        query = event.value.strip()
        if not query:
            return
        if self.search_mode == "skills":
            self.query_one("#search-status", Static).update("  Searching skills...")
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
        rows = self._search_result_rows() if self.search_mode == "mcps" else self._search_skill_rows()
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

    # -- Search skills support --

    def _do_search_skills(self, query: str):
        results = search_skills(query, cwd=self.cwd)
        installed = get_installed_skills(cwd=self.cwd)
        self.call_from_thread(self._show_skills_search_results, results, installed)

    def _show_skills_search_results(self, results: list, installed: list[dict]):
        installed_names = {s["name"] for s in installed}
        container = self.query_one("#search-results", ScrollableContainer)
        container.remove_children()
        self.search_cursor = 0

        if not results:
            self.query_one("#search-status", Static).update("  No skills found. Try a different query.")
            return

        self.query_one("#search-status", Static).update(
            f"  [bold]{len(results)} skills found[/bold]"
        )
        for s in results[:30]:
            row = SkillSearchRow(
                skill_name=s.name,
                source=s.source,
                installs=s.installs,
                installed=s.name in installed_names,
            )
            container.mount(row)
        self._set_search_cursor(0)
        try:
            inp = self.query_one("#search-input", Input)
            if inp.has_focus:
                inp.blur()
                self._input_focused = False
        except Exception:
            pass

    def _search_skill_rows(self) -> list[SkillSearchRow]:
        return list(self.query_one("#search-results", ScrollableContainer).query(SkillSearchRow))

    def _show_skill_search_detail(self):
        """Show detail for a skill search result."""
        rows = self._search_skill_rows()
        if not rows or self.search_cursor >= len(rows):
            return
        row = rows[self.search_cursor]

        def handle_result(result):
            if result == "add_user":
                self._install_skill(row.skill_name, row.source, scope="global")
            elif result == "add_project":
                self._install_skill(row.skill_name, row.source, scope="project")

        self.push_screen(SkillSearchDetailScreen(
            skill_name=row.skill_name,
            source=row.source,
            installs=row.installs,
            installed=row.installed,
            description=row.description,
        ), handle_result)

    def _toggle_search_mode(self):
        """Toggle between MCP and skills search modes."""
        self.search_mode = "skills" if self.search_mode == "mcps" else "mcps"
        self._update_search_placeholder()
        container = self.query_one("#search-results", ScrollableContainer)
        container.remove_children()
        self.search_cursor = 0
        self.query_one("#search-status", Static).update("  [dim]Type a query and press Enter to search[/dim]")
        self.query_one("#search-sub-nav", Label).update(self._render_search_sub_nav())
        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        self.query_one("#footer-hints", Label).update(self._footer_for_tab())
        # Update column header for mode
        col_header = self.query_one("#search-col-header", Label)
        if self.search_mode == "skills":
            col_header.update("  [bold]✓[/]  name                       [bold]installs[/]  source / description")
        else:
            col_header.update("  [bold]✓[/]  name                       [bold]type[/]   registry / description")
        inp = self.query_one("#search-input", Input)
        inp.value = ""

    def _update_search_placeholder(self):
        inp = self.query_one("#search-input", Input)
        if self.search_mode == "skills":
            inp.placeholder = "Search skills (skills.sh)..."
        else:
            inp.placeholder = "Search MCP servers (GitHub MCP org + Registry)..."

    def _toggle_discover_mode(self):
        """Toggle between MCP and skills discover modes."""
        self.discover_mode = "skills" if self.discover_mode == "mcps" else "mcps"
        container = self.query_one("#discover-results", ScrollableContainer)
        container.remove_children()
        self.discover_cursor = 0
        self.query_one("#discover-sub-nav", Label).update(self._render_discover_sub_nav())
        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        # Update column header for mode
        col_header = self.query_one("#discover-col-header", Label)
        if self.discover_mode == "skills":
            col_header.update("  [bold]✓[/]  name                       [bold]installs[/]  source / description")
        else:
            col_header.update(f"     {'name':<{NAME_WIDTH}}     [bold]★[/]  [bold]lang[/]  description")
        if self.discover_mode == "mcps":
            self.discover_view = "categories"
            self._show_categories()
        else:
            self._show_skills_discover()
        self.query_one("#footer-hints", Label).update(self._footer_for_tab())

    def _show_skills_discover(self):
        """Show discoverable skills in the discover tab."""
        self.query_one("#discover-status", Static).update(
            "  [bold]Discover skills[/bold]  [dim]loading...[/dim]"
        )
        thread = Thread(target=self._do_fetch_skills_discover, daemon=True)
        thread.start()

    def _do_fetch_skills_discover(self):
        skills = fetch_skills_discover(count=75)
        installed = get_installed_skills(cwd=self.cwd)
        self.call_from_thread(self._show_discover_skills_results, skills, installed)

    def _show_discover_skills_results(self, skills: list, installed: list[dict]):
        installed_names = {s["name"] for s in installed}
        container = self.query_one("#discover-results", ScrollableContainer)
        container.remove_children()
        self.discover_cursor = 0

        filtered = [s for s in skills if s.name not in installed_names]
        if not filtered:
            self.query_one("#discover-status", Static).update(
                "  [bold]Discover skills[/bold]  [dim]no new skills found[/dim]"
            )
            return

        self.query_one("#discover-status", Static).update(
            f"  [bold]Discover skills[/bold]  [dim]{len(filtered)} available[/dim]"
        )
        for s in filtered:
            row = SkillSearchRow(
                skill_name=s.name,
                source=s.source,
                installs=s.installs,
                installed=False,
            )
            container.mount(row)
        self._set_discover_cursor(0)

    def _navigable_discover_skill_rows(self) -> list[SkillSearchRow]:
        container = self.query_one("#discover-results", ScrollableContainer)
        return [w for w in container.children if isinstance(w, SkillSearchRow)]

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
                self._switch_tab("skills")
                event.stop()
                event.prevent_default()
            return

        # -- Search tab --
        if self.active_tab == "search":
            # tab always toggles MCPs/Skills mode
            if event.key == "tab":
                self._toggle_search_mode()
                event.stop()
                event.prevent_default()
                return

            if is_input:
                inp = self.query_one("#search-input", Input)
                has_rows = (self._search_result_rows() if self.search_mode == "mcps"
                           else self._search_skill_rows())
                if event.key == "down":
                    if has_rows:
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
                    self._switch_tab("skills")
                    event.stop()
                    event.prevent_default()
                elif event.key == "escape":
                    if inp.value:
                        inp.value = ""
                    else:
                        self._switch_tab("skills")
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
                if self.search_mode == "skills":
                    self._show_skill_search_detail()
                else:
                    self._show_search_detail()
            elif event.key == "u" and self.search_mode == "skills":
                rows = self._search_skill_rows()
                if rows and self.search_cursor < len(rows):
                    row = rows[self.search_cursor]
                    if not row.installed:
                        self._install_skill(row.skill_name, row.source, scope="global")
            elif event.key == "p" and self.search_mode == "skills":
                rows = self._search_skill_rows()
                if rows and self.search_cursor < len(rows):
                    row = rows[self.search_cursor]
                    if not row.installed:
                        self._install_skill(row.skill_name, row.source, scope="project")
            elif event.key in ("q", "escape"):
                self._switch_tab("servers")
            event.stop()
            event.prevent_default()
            return

        # -- Discover tab --
        if self.active_tab == "discover":
            # tab always toggles MCPs/Skills mode
            if event.key == "tab":
                self._toggle_discover_mode()
                event.stop()
                event.prevent_default()
                return

            if self.discover_mode == "skills":
                # Skills discover mode
                if event.key in ("j", "down"):
                    rows = self._navigable_discover_skill_rows()
                    if rows:
                        self.discover_cursor = min(self.discover_cursor + 1, len(rows) - 1)
                        for r in rows:
                            r.focused = False
                        rows[self.discover_cursor].focused = True
                        rows[self.discover_cursor].scroll_visible()
                elif event.key in ("k", "up"):
                    rows = self._navigable_discover_skill_rows()
                    if rows:
                        self.discover_cursor = max(0, self.discover_cursor - 1)
                        for r in rows:
                            r.focused = False
                        rows[self.discover_cursor].focused = True
                        rows[self.discover_cursor].scroll_visible()
                elif event.key == "enter":
                    rows = self._navigable_discover_skill_rows()
                    if rows and self.discover_cursor < len(rows):
                        row = rows[self.discover_cursor]
                        def handle_result(result, _row=row):
                            if result == "add_user":
                                self._install_skill(_row.skill_name, _row.source, scope="global")
                            elif result == "add_project":
                                self._install_skill(_row.skill_name, _row.source, scope="project")
                        self.push_screen(SkillSearchDetailScreen(
                            skill_name=row.skill_name,
                            source=row.source,
                            installs=row.installs,
                            installed=row.installed,
                        ), handle_result)
                elif event.key == "u":
                    rows = self._navigable_discover_skill_rows()
                    if rows and self.discover_cursor < len(rows):
                        row = rows[self.discover_cursor]
                        if not row.installed:
                            self._install_skill(row.skill_name, row.source, scope="global")
                elif event.key == "p":
                    rows = self._navigable_discover_skill_rows()
                    if rows and self.discover_cursor < len(rows):
                        row = rows[self.discover_cursor]
                        if not row.installed:
                            self._install_skill(row.skill_name, row.source, scope="project")
                elif event.key in ("left", "h"):
                    self._switch_tab("search")
                elif event.key in ("q", "escape"):
                    self._switch_tab("servers")
            else:
                # MCP discover mode
                if event.key in ("j", "down"):
                    self._set_discover_cursor(self.discover_cursor + 1)
                elif event.key in ("k", "up"):
                    self._set_discover_cursor(self.discover_cursor - 1)
                elif event.key == "enter":
                    self._discover_enter()
                elif event.key in ("left", "h"):
                    self._switch_tab("search")
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
            if event.key in ("j", "down"):
                self._set_skills_cursor(self.skills_cursor + 1)
            elif event.key in ("k", "up"):
                self._set_skills_cursor(max(0, self.skills_cursor - 1))
            elif event.key == "enter":
                self._skills_enter()
            elif event.key == "u":
                rows = self._navigable_skill_rows()
                current = rows[self.skills_cursor] if rows and self.skills_cursor < len(rows) else None
                if isinstance(current, SkillRow):
                    if current.in_user or current.in_project:
                        # Toggle user scope for installed skill
                        if not current.in_user:
                            self._install_skill(current.skill_name, current.source, scope="global")
                        else:
                            # Removing from user scope - only if also in project
                            if current.in_project:
                                self._remove_skill_scope(current, "global")
                            else:
                                self._notify(
                                    "  [dim]Can't remove from user scope — it's the only scope[/dim]"
                                )
            elif event.key == "p":
                rows = self._navigable_skill_rows()
                current = rows[self.skills_cursor] if rows and self.skills_cursor < len(rows) else None
                if isinstance(current, SkillRow):
                    if current.in_user or current.in_project:
                        if not current.in_project:
                            self._install_skill(current.skill_name, current.source, scope="project")
                        else:
                            if current.in_user:
                                self._remove_skill_scope(current, "project")
                            else:
                                self._notify(
                                    "  [dim]Can't remove from project scope — it's the only scope[/dim]"
                                )
            elif event.key == "d":
                rows = self._navigable_skill_rows()
                current = rows[self.skills_cursor] if rows and self.skills_cursor < len(rows) else None
                if isinstance(current, SkillRow):
                    self._remove_skill(current)
            elif event.key in ("e", "space"):
                rows = self._navigable_skill_rows()
                current = rows[self.skills_cursor] if rows and self.skills_cursor < len(rows) else None
                if isinstance(current, SkillRow):
                    self._toggle_skill_enabled(current)
            elif event.key == "s":
                # No-op for skills (changes are applied immediately)
                self._notify("  [dim]Skills changes are saved automatically[/dim]")
            elif event.key in ("left", "h"):
                self._switch_tab("servers")
            elif event.key in ("right",):
                self._switch_tab("search")
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
            "desktop": "Claude Desktop MCPs",
            "desktop_ext": "Claude Desktop extensions",
        }
        cat_hints = {
            "cloud": "manage at [bold]claude.ai/settings[/bold] or [bold]/mcp[/bold] in Claude Code",
            "user_mcp": "edit directly or [bold]claude mcp remove <name> -s user[/bold]",
            "desktop": "press [bold]Enter[/bold] to view, [bold]I[/bold] to import into store",
            "desktop_ext": "managed by Claude Desktop",
        }

        for cat_key, entries in cats.items():
            yield Label(
                f" [dim]── {cat_labels[cat_key]} ──[/dim]",
                classes="readonly-header",
            )
            for name, status in sorted(entries.items()):
                # Pass config for desktop MCPs so they can be imported
                cfg = None
                tc = self._tool_count_for(name)
                if cat_key == "desktop":
                    cfg = self.desktop_mcp_configs.get(name, {})
                elif cat_key == "desktop_ext":
                    ext = self.desktop_extensions.get(name, {})
                    tc = ext.get("tool_count", 0) or tc
                yield ReadOnlyRow(name, status, tool_count=tc, mcp_cfg=cfg)
            yield Label(
                f"  [dim]{cat_hints[cat_key]}[/dim]",
                classes="readonly-hint",
            )

    def _focusable_rows(self) -> list[MCPRow | PluginRow | ReadOnlyRow]:
        container = self.query_one("#mcp-list", ScrollableContainer)
        return [
            w for w in container.children
            if isinstance(w, (MCPRow, PluginRow, ReadOnlyRow))
        ]

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

    def _current_row(self) -> MCPRow | PluginRow | ReadOnlyRow | None:
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
            if isinstance(row, MCPRow):
                def handle_result(result):
                    if result == "delete":
                        self._confirm_delete()

                all_tools = get_mcp_tool_names(self.claude_dir)
                permitted = all_tools.get(row.mcp_name, all_tools.get(row.mcp_name.replace("-", "_"), []))
                self.push_screen(DetailScreen(row.mcp_name, row.mcp_cfg, permitted_tools=permitted), handle_result)
            elif isinstance(row, PluginRow):
                all_tools = get_mcp_tool_names(self.claude_dir)
                # Plugin tools use pattern: mcp__plugin_{Name}_{name}__tool
                name_key = f"plugin_{row.display_name}_{row.display_name}"
                permitted = all_tools.get(name_key, [])

                def handle_plugin_result(result, _row=row):
                    if result == "toggle":
                        _row.toggle_enabled()
                    elif result == "delete":
                        self._confirm_delete()

                self.push_screen(
                    PluginDetailScreen(row.plugin_id, row.enabled, self.claude_dir, permitted_tools=permitted),
                    handle_plugin_result,
                )
            elif isinstance(row, ReadOnlyRow):
                all_tools = get_mcp_tool_names(self.claude_dir)
                # Try different name patterns for cloud connectors
                name_key = row.mcp_name.replace("claude.ai ", "claude_ai_").replace(" ", "_")
                permitted = all_tools.get(name_key, all_tools.get(row.mcp_name.replace("-", "_"), []))
                importable = row.status.startswith("desktop") and not row.status.endswith("extension")
                needs_auth = row.status == "needs auth"

                def handle_readonly_result(result, _row=row):
                    if result == "import" and _row.mcp_cfg:
                        self._import_desktop_mcp(_row.mcp_name, _row.mcp_cfg)
                    elif result == "auth_started":
                        self.notify(f"Authenticating {_row.mcp_name} — check your browser")

                claude_json = self.claude_json if self.claude_json else None
                self.push_screen(
                    DetailScreen(row.mcp_name, row.mcp_cfg, permitted_tools=permitted, readonly=True, importable=importable, needs_auth=needs_auth, claude_dir=self.claude_dir, claude_json=claude_json),
                    handle_readonly_result,
                )

    def action_import_desktop(self):
        if self.active_tab != "servers":
            return
        row = self._current_row()
        if isinstance(row, ReadOnlyRow) and row.status == "desktop" and row.mcp_cfg:
            self._import_desktop_mcp(row.mcp_name, row.mcp_cfg)

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
        total_focusable = len(self._focusable_rows())
        self._set_cursor(min(self.cursor, max(0, total_focusable - 1)))

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
