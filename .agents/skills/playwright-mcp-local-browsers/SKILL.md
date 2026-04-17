---
name: playwright-mcp-local-browsers
description: Set up project-local Playwright browsers and configure Claude Code's Playwright MCP to use them via PLAYWRIGHT_BROWSERS_PATH. Use when the user wants the Playwright MCP server (mcp__playwright__*) to launch a browser stored inside the current project rather than the global ~/Library/Caches/ms-playwright cache, or when the user complains about MCP failing because of chromium revision mismatches against project-installed Playwright.
---

# Playwright MCP — project-local browsers

Goal: one browser directory per project. Both the project's own Playwright (if any) and Claude Code's Playwright MCP point at it. MCP config holds no version-pinned paths, so it survives dependency upgrades.

## Approach (simple, accept duplicate on pnpm)

- Browsers live at `<browser-dir>/.playwright-browsers/` (gitignored)
- The Playwright MCP entry in `~/.claude.json` carries `env.PLAYWRIGHT_BROWSERS_PATH=<absolute-browser-dir>`
- If the project also runs Playwright e2e, **two** chromium revisions coexist in the same dir (project's revision + MCP's revision). Each Playwright instance auto-discovers its matching `chromium-<rev>/` subdir.
- pnpm caveat: pnpm doesn't propagate `.npmrc` keys as env vars, so its `pnpm install` postinstall hook will still drop a duplicate copy into `node_modules/.pnpm/playwright-core@*/.../.local-browsers/`. **Don't try to fix this with `.npmrc`.** It's gitignored disk waste; live with it.

## Steps

Execute the steps below as the agent, in order. Use absolute paths everywhere.

### 1. Resolve `PROJECT_ROOT` and `BROWSER_DIR_PARENT`

- `PROJECT_ROOT` = the project the user is configuring. Use cwd if the user said "this project"; otherwise ask.
- Find the package.json that depends on `playwright` or `@playwright/test`:
  ```bash
  find "$PROJECT_ROOT" -maxdepth 4 -name package.json -not -path "*/node_modules/*" \
    -exec grep -l '"@playwright/test"\|"playwright"' {} \;
  ```
- If a match exists, `BROWSER_DIR_PARENT` = the directory containing that package.json.
- If no match (project doesn't use Playwright itself, only needs MCP), `BROWSER_DIR_PARENT` = `PROJECT_ROOT`.
- Define `BROWSER_DIR="$BROWSER_DIR_PARENT/.playwright-browsers"`.

### 2. Find MCP's bundled Playwright CLI

The Playwright MCP server (`@playwright/mcp`) ships its own `playwright-core`. Its bundled chromium revision usually differs from the project's. Find it:

```bash
# Trigger npx cache population if not yet present
npx -y @playwright/mcp@latest --version >/dev/null 2>&1 || true

# Locate the cache dir
MCP_PLAYWRIGHT_CLI=$(find ~/.npm/_npx -path "*@playwright/mcp/package.json" -print -quit \
  | sed 's|/@playwright/mcp/package.json|/.bin/playwright|')
```

Verify `MCP_PLAYWRIGHT_CLI` exists and is executable.

### 3. Install browsers into `BROWSER_DIR`

Always install MCP's revision:
```bash
PLAYWRIGHT_BROWSERS_PATH="$BROWSER_DIR" "$MCP_PLAYWRIGHT_CLI" install chromium
```

If the project itself uses Playwright (step 1 found a match), also install the project's revision. Detect the package manager from lockfiles in `BROWSER_DIR_PARENT`:
- `pnpm-lock.yaml` → `pnpm exec`
- `yarn.lock` → `yarn exec`
- `package-lock.json` → `npx`

```bash
cd "$BROWSER_DIR_PARENT"
PLAYWRIGHT_BROWSERS_PATH="$BROWSER_DIR" <pkg-mgr> exec playwright install chromium
```

After both runs, `ls "$BROWSER_DIR"` should show `chromium-<revA>/` and (usually) a different `chromium-<revB>/`. If only one revision is shown, both Playwrights happen to share it — fine.

### 4. Add `.gitignore` entry

In `BROWSER_DIR_PARENT/.gitignore`, append `.playwright-browsers` if not already present. If `.gitignore` doesn't exist, create it.

### 5. Update `~/.claude.json` MCP config

Back up first:
```bash
cp ~/.claude.json ~/.claude.json.bak.$(date +%Y%m%d-%H%M%S)
```

Patch via Python (preserves all unrelated state, doesn't touch formatting of other keys):
```python
import json, pathlib

CONFIG = pathlib.Path.home() / ".claude.json"
PROJECT_ROOT = "<absolute project root>"
BROWSER_DIR = "<absolute browser dir>"

data = json.loads(CONFIG.read_text())
projects = data.setdefault("projects", {})
proj = projects.setdefault(PROJECT_ROOT, {})
servers = proj.setdefault("mcpServers", {})
servers["playwright"] = {
    "type": "stdio",
    "command": "npx",
    "args": ["@playwright/mcp@latest"],
    "env": {"PLAYWRIGHT_BROWSERS_PATH": BROWSER_DIR},
}
CONFIG.write_text(json.dumps(data, indent=2) + "\n")
```

If a previous config had `--executable-path` in `args`, **replace** the entire entry with the env-var form above. Don't mix the two — `--executable-path` to a chromium revision the MCP's playwright doesn't expect causes silent SIGTRAP on launch.

### 6. Tell the user to reload MCP

The currently running MCP server in this Claude Code session still uses the old config. The user must either:
- Restart Claude Code, or
- Run `/mcp` and reconnect the playwright server (UI-dependent)

`/reload-plugins` does **not** restart MCP servers.

### 7. Verify

After reload, call `mcp__playwright__browser_navigate` to a known-good URL (e.g. `about:blank`) and `mcp__playwright__browser_evaluate` to fetch `navigator.userAgent`. Browser should launch from `$BROWSER_DIR/chromium-<rev>/...`.

If launch fails with SIGTRAP and config-injected env shows the right path, the chromium revision in `$BROWSER_DIR` doesn't match what MCP's playwright expects — re-run step 3's MCP install. This usually means MCP got upgraded server-side.

## Maintenance

- **Project upgrades its Playwright** → re-run step 3 second sub-command.
- **MCP gets a new chromium revision** (rare; tied to `@playwright/mcp` major bumps) → re-run step 3 first sub-command. The old revision dir can be deleted manually.
- **Disk pressure** → safe to delete unused `chromium-<rev>/` and `chromium_headless_shell-<rev>/` subdirs in `BROWSER_DIR`; they re-download on next install.

## Anti-patterns (don't do)

- **Hardcoding `--executable-path` in MCP config**: brittle to revision drift, causes silent SIGTRAP when revisions disagree. Use `env.PLAYWRIGHT_BROWSERS_PATH` instead.
- **Trying `.npmrc` with pnpm**: pnpm does not propagate `.npmrc` keys as env vars to lifecycle scripts (unlike npm). It silently won't work; the postinstall still installs to `node_modules/.pnpm/.../.local-browsers/`.
- **Using `${PROJECT_CWD}` in `.npmrc`**: not a pnpm-supported placeholder. pnpm prints `Failed to replace env in config` and ignores the line.
- **Pointing the dashboard's Playwright at MCP's chromium revision (or vice versa)**: protocol mismatch → SIGTRAP. Keep both, let each instance pick its own.
