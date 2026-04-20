# @polaris/welcome-page

Standalone Polaris welcome page rendered inside the per-project `chromium-vnc`
container. Used both as the Chromium startup URL (`file:///config/welcome.html`)
and as the new-tab override (via a tiny unpacked MV3 extension).

## Build

```bash
pnpm --filter @polaris/welcome-page build
```

Output:

- `dist/welcome.html` — fully self-contained (CSS inlined; no external requests).
- `dist/extension/manifest.json` — MV3 manifest with `chrome_url_overrides.newtab`.
- `dist/extension/welcome.html` — same compiled HTML the manifest points at.
- `dist/extension/icon-128.png` — placeholder icon.

## How it's consumed

`apps/api` copies `dist/welcome.html` and `dist/extension/` into
`<meta_path>/browser-config/` on every workspace-runtime start, mounts
that directory at `/config` inside the chromium container, and passes
`--load-extension=/config/extension file:///config/welcome.html` via
`CHROME_CLI`. If `dist/` is missing, the API degrades to `about:blank`.
