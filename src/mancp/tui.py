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

from mancp.registry import MCPServerResult, is_in_store, search_all
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
    KNOWN_TOOL_COUNTS,
    load_settings,
    mask_secrets,
    remove_mcp_everywhere,
    save_settings,
    save_store,
    apply_changes,
    token_warning_level,
    CLAUDE_DIR,
    SETTINGS_JSON,
    STORE_FILE,
)

NAME_WIDTH = 26
BADGE_WIDTH = 4  # right-aligned token badge column


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
        # Align: 2 spaces + indicator + 5 spaces (skip U/P cols) + name + badge + status
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

        # Align: cursor + indicator + 5 spaces (skip P col) + name + badge + source
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
        stars = f"[dim]{self.result.stars:>4}★[/]" if self.result.stars else f"{'':>5}"

        padded_name = self.result.name.ljust(NAME_WIDTH)
        if self.focused:
            padded_name = f"[bold]{padded_name}[/bold]"
        else:
            padded_name = f"[#6b7280]{padded_name}[/]"

        desc = self.result.description[:46]
        return f"{cursor}{installed}  {padded_name} {stars}  [dim]{desc}[/dim]"


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

    TABS = ["servers", "marketplace"]

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
        self.names = sorted(store.keys())
        self.plugin_ids = sorted(self.plugins.keys())
        self.cursor = 0
        self.active_tab = "servers"
        self.search_cursor = 0
        self.search_results: list[MCPServerResult] = []
        self._input_focused = False

    def compose(self) -> ComposeResult:
        cwd_name = self.cwd.name
        yield Label(self._render_tab_bar(cwd_name), id="tab-bar")

        # -- Servers tab --
        yield Label(
            "  [bold]U[/]  [bold]P[/]  name                       [bold]~tok[/]  command / url",
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

        # -- Marketplace tab (hidden initially, input disabled) --
        with Vertical(id="search-panel", classes="hidden"):
            yield Input(placeholder="Search MCP servers (npm + GitHub)...", id="search-input", disabled=True)
            yield Label(
                "  [bold]✓[/]  name                       [bold]stars[/]  description",
                id="search-col-header",
            )
            yield Static("  [dim]Type a query and press Enter to search[/dim]", id="search-status")
            yield ScrollableContainer(id="search-results", can_focus=False)

        yield Static("", id="status-bar")
        yield Label(self._footer_for_tab(), id="footer-hints")

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
                "[dim]s[/dim] save   [dim]→[/dim] marketplace   [dim]q[/dim] quit"
            )
        return (
            "  [dim]type[/dim] to search   [dim]tab[/dim] results   "
            "[dim]j/k[/dim] navigate   [dim]enter[/dim] add to store   "
            "[dim]←[/dim] servers   [dim]q[/dim] quit"
        )

    def _switch_tab(self, tab: str):
        if tab == self.active_tab:
            return
        self.active_tab = tab

        mcp_list = self.query_one("#mcp-list", ScrollableContainer)
        col_header = self.query_one("#col-header", Label)
        search_panel = self.query_one("#search-panel", Vertical)

        search_input = self.query_one("#search-input", Input)
        if tab == "servers":
            mcp_list.remove_class("hidden")
            col_header.remove_class("hidden")
            search_panel.add_class("hidden")
            # Blur and disable input so it can't steal keys
            if search_input.has_focus:
                search_input.blur()
            search_input.disabled = True
            self._input_focused = False
        else:
            mcp_list.add_class("hidden")
            col_header.add_class("hidden")
            search_panel.remove_class("hidden")
            # Enable and focus search input
            search_input.disabled = False
            search_input.focus()
            self._input_focused = True

        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        self.query_one("#footer-hints", Label).update(self._footer_for_tab())

    # -- Search input handlers --

    def on_input_submitted(self, event: Input.Submitted):
        query = event.value.strip()
        if not query:
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

        gh_count = sum(1 for r in results if r.source == "github")
        npm_count = len(results) - gh_count
        self.query_one("#search-status", Static).update(
            f"  {len(results)} results  [dim]({gh_count} github, {npm_count} npm)[/dim]"
        )

        shown_gh_header = False
        shown_npm_header = False
        row_idx = 0
        for r in results:
            # Section headers
            if r.source == "github" and not shown_gh_header:
                container.mount(Label(
                    " [dim]── github repositories ──[/dim]",
                    classes="readonly-header",
                ))
                shown_gh_header = True
            elif r.source == "npm" and not shown_npm_header:
                container.mount(Label(
                    " [dim]── npm packages ──[/dim]",
                    classes="readonly-header",
                ))
                shown_npm_header = True

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
        # Mark as in-store in the search results
        rows[self.search_cursor].in_store = True
        rows[self.search_cursor].refresh()
        # Rebuild servers list (will show when switching back)
        self._rebuild_list()
        self.query_one("#tab-bar", Label).update(self._render_tab_bar())
        self.query_one("#status-bar", Static).update(
            f"  + {name} ({result.package}) added to store"
        )

    def on_key(self, event):
        focused = self.focused
        is_input = isinstance(focused, Input)
        self._input_focused = is_input

        # -- Servers tab: left/right switch tabs --
        if self.active_tab == "servers":
            if event.key in ("right", "l"):
                self._switch_tab("marketplace")
                event.stop()
                event.prevent_default()
            return  # let normal bindings handle j/k/u/p/etc

        # -- Marketplace tab --
        if is_input:
            # Input has focus — let most keys pass through
            if event.key in ("tab", "down"):
                if self._search_result_rows():
                    self.query_one("#search-input", Input).blur()
                    self._input_focused = False
                    self._set_search_cursor(self.search_cursor)
                    event.stop()
                    event.prevent_default()
            elif event.key == "escape":
                inp = self.query_one("#search-input", Input)
                if inp.value:
                    inp.value = ""
                else:
                    self._switch_tab("servers")
                event.stop()
                event.prevent_default()
            return  # don't process app bindings while typing

        # Marketplace tab, results browsing (input not focused)
        if event.key in ("j", "down"):
            self._set_search_cursor(self.search_cursor + 1)
        elif event.key in ("k", "up"):
            if self.search_cursor == 0:
                # At top of results — jump back to input
                self.query_one("#search-input", Input).focus()
                self._input_focused = True
            else:
                self._set_search_cursor(self.search_cursor - 1)
        elif event.key == "enter":
            self._add_search_result()
        elif event.key in ("left", "h"):
            self._switch_tab("servers")
        elif event.key == "tab":
            self.query_one("#search-input", Input).focus()
            self._input_focused = True
        elif event.key in ("q", "escape"):
            self._switch_tab("servers")
        event.stop()
        event.prevent_default()

    # -- Server tab helpers --

    def _tool_count_for(self, name: str, cfg: dict | None = None) -> int:
        """Estimate tool count using known lookups + permission data."""
        if cfg is not None:
            return estimate_tool_count(name, cfg, self.tool_counts)
        # For readonly/plugin entries without a cfg, try direct lookup
        if name in KNOWN_TOOL_COUNTS:
            return KNOWN_TOOL_COUNTS[name]
        if name in self.tool_counts:
            return self.tool_counts[name]
        alt = name.replace("-", "_")
        if alt in self.tool_counts:
            return self.tool_counts[alt]
        return 0

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
            # Try multiple key patterns for plugin tool counts
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
        # Skip plugin category — handled by _plugin_widgets
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
        """All navigable rows: MCPRows then PluginRows."""
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
            # Remove from all scopes: ~/.claude.json (global + all projects) + .mcp.json
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
        # MCP changes
        mcp_rows = self._rows()
        user_mcps = {r.mcp_name for r in mcp_rows if r.in_user}
        project_mcps = {r.mcp_name for r in mcp_rows if r.in_project}
        kwargs = {"claude_json": self.claude_json} if self.claude_json else {}
        mcp_msg = apply_changes(self.store, user_mcps, project_mcps, self.cwd, **kwargs)
        self.user_active = user_mcps
        self.project_active = project_mcps

        # Plugin changes
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
        if self.active_tab == "marketplace":
            self._switch_tab("servers")
            return
        self.exit()
