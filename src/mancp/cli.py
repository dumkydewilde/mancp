"""CLI entry point for mancp."""

import argparse
import sys
from pathlib import Path

from mancp.store import (
    STORE_FILE,
    collect_all_from_claude_json,
    get_plugins,
    get_project_scope_mcps,
    get_user_scope_mcps,
    load_claude_json,
    load_store,
    save_claude_json,
    save_store,
    set_plugin_enabled,
)
from mancp.registry import is_in_store, search_all


def cmd_init() -> None:
    existing = load_store()
    found = collect_all_from_claude_json()
    if not found:
        print("No MCP servers found in ~/.claude.json")
        return
    added, skipped = [], []
    for name, cfg in found.items():
        if name in existing:
            skipped.append(name)
        else:
            existing[name] = cfg
            added.append(name)
    save_store(existing)
    print(f"Imported {len(added)} MCP(s) -> {STORE_FILE}")
    for n in added:
        print(f"  + {n}")
    if skipped:
        print(f"  (skipped {len(skipped)} already present: {', '.join(skipped)})")


def cmd_list() -> None:
    store = load_store()
    if not store:
        print("No MCPs in store. Run `mancp init` to import from ~/.claude.json")
        return
    user_active = get_user_scope_mcps()
    project_active = get_project_scope_mcps(Path.cwd())
    print(f"Store: {STORE_FILE}  ({len(store)} entries)\n")
    for name in sorted(store.keys()):
        cfg = store[name]
        desc = cfg.get("command", cfg.get("url", "?"))
        u = "[U]" if name in user_active else "   "
        p = "[P]" if name in project_active else "   "
        print(f"  {u}{p}  {name:<30}  {desc}")
    print("\n  [U] = active in ~/.claude.json user scope")
    print("  [P] = active in current project .mcp.json")


def cmd_clean() -> None:
    data = load_claude_json()
    g = len(data.get("mcpServers", {}))
    p = sum(
        len(pd.get("mcpServers", {}))
        for pd in data.get("projects", {}).values()
    )
    if g == 0 and p == 0:
        print("Nothing to clean -- no MCP servers in ~/.claude.json")
        return
    print(f"Remove {g} global + {p} project-scoped MCP(s) from ~/.claude.json?")
    print("(A timestamped backup will be created)")
    if input("Continue? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return
    data["mcpServers"] = {}
    for path in data.get("projects", {}):
        data["projects"][path]["mcpServers"] = {}
    save_claude_json(data)
    print("Cleaned ~/.claude.json  (backup saved)")


def cmd_plugins(args) -> None:
    plugins = get_plugins()
    if not plugins:
        print("No plugins found in ~/.claude/settings.json")
        return

    if args.toggle:
        name = args.toggle
        # Find matching plugin by short name
        match = None
        for pid in plugins:
            if pid.split("@")[0] == name or pid == name:
                match = pid
                break
        if not match:
            print(f"Plugin '{name}' not found. Available: {', '.join(p.split('@')[0] for p in plugins)}")
            return
        new_state = not plugins[match]
        set_plugin_enabled(match, new_state)
        state_str = "enabled" if new_state else "disabled"
        print(f"  {match.split('@')[0]}: {state_str}")
        return

    enabled = sum(1 for v in plugins.values() if v)
    print(f"Plugins: {enabled}/{len(plugins)} enabled\n")
    for pid, on in sorted(plugins.items()):
        name = pid.split("@")[0]
        source = pid.split("@")[1] if "@" in pid else ""
        indicator = "[on] " if on else "[off]"
        print(f"  {indicator}  {name:<30}  {source}")
    print("\n  Toggle: mancp plugins --toggle <name>")


def cmd_search(query: str) -> None:
    print(f"Searching for '{query}'...\n")
    results = search_all(query)
    if not results:
        print("No MCP servers found. Try a different query.")
        return

    store = load_store()
    for i, r in enumerate(results, 1):
        in_store = " [installed]" if is_in_store(r, store) else ""
        src = f"[{r.source}]"
        stars = f" {r.stars}*" if r.stars else ""
        print(f"  {i:>2}. {r.name:<28} {src:<8}{stars}")
        print(f"      {r.description[:70]}")
        print(f"      pkg: {r.package}{in_store}")
        print()

    print("Add to store: mancp add <number or package-name> --from-search '<query>'")
    print("  or:         mancp add <npm-package-name>")


def cmd_add(args) -> None:
    store = load_store()

    if args.from_search:
        # Re-run search and pick by number
        results = search_all(args.from_search)
        if not results:
            print("Search returned no results.")
            return
        try:
            idx = int(args.name) - 1
            if idx < 0 or idx >= len(results):
                print(f"Pick a number between 1 and {len(results)}")
                return
            result = results[idx]
        except ValueError:
            # Try matching by package name
            result = next((r for r in results if r.package == args.name or r.name == args.name), None)
            if not result:
                print(f"'{args.name}' not found in search results. Use a number or exact package name.")
                return

        name = args.alias or result.name
        cfg = result.to_mcp_config()
        print(f"Adding '{name}' ({result.package}) from {result.source}")
    else:
        # Direct add: assume npm package
        name = args.alias or args.name
        pkg = args.name
        # Clean up name for store key
        if not args.alias:
            for prefix in ("@modelcontextprotocol/server-", "@anthropic/mcp-server-", "@anthropic-ai/mcp-server-"):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            if name.startswith("mcp-server-"):
                name = name[len("mcp-server-"):]
        cfg = {"command": "npx", "args": ["-y", pkg]}
        print(f"Adding '{name}' (npx -y {pkg})")

    if name in store:
        print(f"  '{name}' already in store. Use --alias to pick a different name.")
        return

    store[name] = cfg
    save_store(store)
    print(f"  + {name} -> {STORE_FILE}")
    print(f"  Run `mancp` to toggle it on for your project.")


def cmd_tui(cwd: Path) -> None:
    from mancp.tui import ManCPApp

    store = load_store()
    found = collect_all_from_claude_json()
    new_entries = {k: v for k, v in found.items() if k not in store}
    if new_entries:
        store.update(new_entries)
        save_store(store)

    if not store:
        print("No MCPs in store. Run `mancp init` first.")
        sys.exit(1)

    ManCPApp(store=store, cwd=cwd).run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="mancp -- MCP profile manager for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            "  (none)    Open TUI to select MCPs for current project\n"
            "  init      Import all MCPs from ~/.claude.json into the store\n"
            "  list      List all stored MCPs\n"
            "  search    Search npm & GitHub for MCP servers\n"
            "  add       Add an MCP server to the store\n"
            "  plugins   List and toggle plugins\n"
            "  clean     Remove all MCPs from ~/.claude.json (with backup)\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="Import MCPs from ~/.claude.json into store")
    sub.add_parser("list", help="List all stored MCPs with active status")
    plugins_parser = sub.add_parser("plugins", help="List and toggle plugins")
    plugins_parser.add_argument("--toggle", "-t", metavar="NAME", help="Toggle a plugin on/off")
    sub.add_parser("clean", help="Remove all MCPs from ~/.claude.json")
    search_parser = sub.add_parser("search", help="Search npm & GitHub for MCP servers")
    search_parser.add_argument("query", nargs="+", help="Search terms")
    add_parser = sub.add_parser("add", help="Add an MCP server to the store")
    add_parser.add_argument("name", help="Package name or search result number")
    add_parser.add_argument("--from-search", metavar="QUERY", help="Pick from search results for this query")
    add_parser.add_argument("--alias", metavar="NAME", help="Store name (default: derived from package)")
    args = parser.parse_args()

    if args.command == "init":
        cmd_init()
    elif args.command == "list":
        cmd_list()
    elif args.command == "plugins":
        cmd_plugins(args)
    elif args.command == "clean":
        cmd_clean()
    elif args.command == "search":
        cmd_search(" ".join(args.query))
    elif args.command == "add":
        cmd_add(args)
    else:
        cmd_tui(Path.cwd())


if __name__ == "__main__":
    main()
