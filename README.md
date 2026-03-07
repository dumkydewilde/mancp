# mancp

A lightweight MCP profile manager for Claude Code and Conductor. Pick which MCP servers load per project instead of dumping them all into every session.

## The problem

Claude Code merges user-level MCPs from `~/.claude.json` into every project, always. There's no per-project exclusion. With 10+ MCPs configured, you burn 30-50k tokens on tool definitions before writing a line of code.

**mancp** stores all your MCPs in a central registry and lets you toggle each one independently between user scope (`~/.claude.json`) and project scope (`.mcp.json`).

## Install

```bash
uvx mancp
```

Or install permanently:

```bash
uv tool install mancp
```

## Quick start

```bash
# 1. Import your existing MCPs (once)
mancp init

# 2. Open the TUI in any project
cd ~/repos/my-project
mancp
```

`mancp init` reads all MCP servers from `~/.claude.json` (global + all project scopes) into `~/.config/mancp/mcps.json`. Safe to re-run -- skips duplicates.

## TUI

```
  mancp  |  my-project  |  12 in store
 U  P  name                        command / url
 U  -  github                      npx  -y  @modelcontextprotocol/server-github
 -  P  motherduck                  npx  -y  @motherduck/mcp-server
 U  P  filesystem                  npx  -y  @anthropic/mcp-server-filesystem
 -  -  slack                       npx  -y  @anthropic/mcp-server-slack
  j/k navigate   u user   p project   enter detail   d delete   s save   q quit
```

Each MCP has two independent toggles:

| Key | Action |
|-----|--------|
| `u` | Toggle user scope (`~/.claude.json`) for current row |
| `p` | Toggle project scope (`.mcp.json`) for current row |
| `j` / `k` or arrows | Navigate list |
| `enter` | Open detail view (shows config, secrets masked) |
| `s` / `ctrl+s` | Save all changes |
| `q` / `esc` | Quit without saving |

### Status columns

- `U` (yellow) -- active in `~/.claude.json` user scope (loads in all projects)
- `P` (cyan) -- active in project `.mcp.json` (loads only in this project)
- `-` (dim) -- not active in that scope

An MCP can be in both scopes, neither, or just one. On save, mancp writes `~/.claude.json` and `.mcp.json` to match what you see.

### Detail view

Press `enter` on any row to see the full MCP config (secrets masked). From there:
- `d` -- delete from store permanently
- `q` / `esc` -- close

## Other commands

```bash
mancp list       # Show store with active status indicators
mancp clean      # Remove all MCPs from ~/.claude.json (with confirmation)
```

## Store format

`~/.config/mancp/mcps.json` -- plain JSON, same structure as `.mcp.json`'s `mcpServers` block. You can edit it directly to rename entries or fix configs.

## Notes

- `.mcp.json` should be in `.gitignore` if it contains tokens
- `~/.claude.json` is backed up automatically before every write
- The TUI auto-detects new MCPs added via `claude mcp add` since last init

## Development

```bash
git clone https://github.com/dumkydewilde/mancp
cd mancp
uv sync --dev
uv run pytest
```

## License

MIT
