---
name: polaris-publish
description: Use when the user wants to ship, deploy, publish, or "make this live". Covers scaffolding the deploy manifest, the publish run itself, and rolling back. The platform builds the image, runs a smoke test, and promotes behind traefik with a real cert — you don't write Dockerfiles by hand or push to a registry yourself.
---

# Polaris publish

Three commands cover 90% of the lifecycle: `scaffold-publish`, `publish`, `rollback`.

## Happy path (first publish)

```bash
polaris scaffold-publish                            # menu mode — read what it auto-detects
polaris scaffold-publish --stack=<auto-detected>    # use the stack the menu suggested
# review/edit the three files, commit
git add . && git commit -m "scaffold publish"
polaris publish                                     # build + smoke + promote, log streams via SSE
```

**Always run the menu form first** and pick the auto-detected stack rather than guessing.  The detector reads marker files (Prisma schema, Vite dep, requirements.txt, etc.) — guessing wrong (e.g. picking `node` for a Prisma project) silently produces a Dockerfile that doesn't `prisma generate`, and you'll burn 5+ failed publishes finding out.

Stacks: `spa` (Vite/Astro/CRA → nginx), `node` (Express/Next SSR/Fastify, no Prisma), `node-prisma` (same as node + copies `prisma/` before install + `prisma generate`), `python` (FastAPI/Django/Flask), `static` (pure HTML), `custom` (you author the manifest).

## Dry-run the build locally before `polaris publish`

`polaris publish` runs whatever is in `polaris.yaml::build` inside a fresh production container.  TypeScript / linter / type errors that the dev mode forgives (`next dev`, `vite`, `tsc --noEmit`-not-running) will fail strict-mode prod compile, and each round-trip costs ~30-60s of docker build.  Run the literal build cmd from `polaris.yaml` once locally first to catch them all in one pass:

```bash
# Whatever is in polaris.yaml::build — examples:
npm run build         # node / node-prisma / spa
pnpm build            # ditto if pnpm
python -m build       # python wheel projects
# (static stack has no build step — skip)
```

Fix everything it reports, commit, THEN `polaris publish`.

## Stack-specific gotchas

### Next.js + DB-backed pages (any node-prisma project)

`next build` tries to **prerender** server components by default, including ones that read from your database.  In production build there is **no DB connection** — prerender crashes with "can't connect" or "relation doesn't exist", aborting the whole build.

Mark every server component / page / layout that does a DB read with one of:

```ts
export const dynamic = 'force-dynamic';   // SSR every request
// or, for cacheable content:
export const revalidate = 60;             // ISR, regenerate every N seconds
```

Otherwise the page is treated as static and runs at build time, where your DB doesn't exist.  Goes in `app/<route>/page.tsx`, `app/<route>/layout.tsx`, or any RSC that fetches.

### Strict prod TypeScript

Next.js / Vite production builds run TS with stricter settings than dev.  Implicit `any` on callback params, unused vars in some configs, and missing return types can all pass `dev` and fail `build`.  The dry-run section above is the cheap fix; if you hit one mid-publish, fix locally and re-run dry-run before re-publishing.

`polaris publish` runs `prepublish-audit` automatically — secret scan, size scan, plus an LLM deep-audit on the platform side. Failed audit blocks the build.

## Subsequent publishes

After the first one, scaffolding is done. Just commit and `polaris publish`.

## Rolling back

```bash
polaris rollback <git-commit-hash>   # short hash from `git log` works
```

Redeploys the image tagged with that commit. The image must still be in the registry (we keep recent ones).

## Common pitfalls

- **Modifying the scaffolded files without thinking.** The templates encode platform conventions (port from `polaris.yaml`, env via `secrets.env`, no host-published ports). Tweak the `start` / `build` commands freely; leave the structural pieces alone.
- **Killing `polaris publish` mid-stream.** Ctrl-C only detaches the local SSE viewer — the build keeps running on the platform. Re-attach by hitting the deployment events endpoint, or just check `polaris status`.
- **`--dry-run` does NOT promote.** It builds and smoke-tests but doesn't flip the route. Use it to validate the image; drop the flag for a real publish.

## More

`polaris publish --help`, `polaris scaffold-publish --help`, `polaris rollback --help`, `polaris status`.
