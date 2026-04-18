---
name: polaris-publish
description: Use when the user wants to ship, deploy, publish, or "make this live". Covers scaffolding the deploy manifest, the publish run itself, and rolling back. The platform builds the image, runs a smoke test, and promotes behind traefik with a real cert — you don't write Dockerfiles by hand or push to a registry yourself.
---

# Polaris publish

Three commands cover 90% of the lifecycle: `scaffold-publish`, `publish`, `rollback`.

## Happy path (first publish)

```bash
polaris scaffold-publish              # see menu + auto-detected stack
polaris scaffold-publish --stack=node # writes Dockerfile, compose.prod.yml, polaris.yaml
# review/edit the three files, commit
git add . && git commit -m "scaffold publish"
polaris publish                       # build + smoke + promote, log streams via SSE
```

Stacks: `spa` (Vite/Astro/CRA → nginx), `node` (Express/Next SSR/Fastify), `python` (FastAPI/Django/Flask), `static` (pure HTML), `custom` (you author the manifest).

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
- **Forgetting to commit before publishing.** `publish` builds from the committed tree, not the working directory. Uncommitted changes won't ship.
- **Killing `polaris publish` mid-stream.** Ctrl-C only detaches the local SSE viewer — the build keeps running on the platform. Re-attach by hitting the deployment events endpoint, or just check `polaris status`.
- **`--dry-run` does NOT promote.** It builds and smoke-tests but doesn't flip the route. Use it to validate the image; drop the flag for a real publish.

## More

`polaris publish --help`, `polaris scaffold-publish --help`, `polaris rollback --help`, `polaris status`.
