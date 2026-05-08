# Replay fixtures

Fixtures used by the replay-test harness (`feat/replay-test-harness`).
They drive `ReplayCodexSession` and `ReplayDesignIntentRunner` so the
worker can satisfy a recorded scenario without hitting OpenAI /
Pinterest / image-gen.

## What's in this directory

```
raw/
  _dummy.json            ← committed, schema-validator self-test
  *.json[.gz]            ← gitignored, real recordings
annotated/
  _dummy.json            ← committed
  *.json                 ← annotation layers (committed when present)
assets/
  .gitkeep               ← committed
  *-workspace.tar.gz     ← gitignored, post-build /workspace snapshot
                           paired with a raw fixture
```

## Why real recordings are gitignored

The recorder captures node outputs verbatim — that includes:

* internal docker-network IP addresses leaked into vite stderr
* `planType` from the codex `account/rateLimits/updated` notification
* URLs of internal Pinterest proxy on `pinterest_refs[].max/.normal`

None of it is a credential; we audited.  But shipping infrastructure
coordinates by default isn't worth the risk if/when the repo opens
up.  Operators record locally and replay tests skip cleanly when the
fixture is missing.

## Recording (one-time per scenario)

```bash
# 1. Set the recorder path on api+worker, restart them
echo 'POLARIS_RECORD=/home/sun/projects/polaris-project/tests/fixtures/replay/raw/<scenario>.json.gz' >> .env
docker compose -f compose.dev.yaml up -d --force-recreate api worker

# 2. Drive the scenario in the browser (or via Playwright MCP).  Each
#    user click should be POSTed to /replay/record/append; codex
#    frames + design-intent node outputs auto-flush via the taps.

# 3. Finalize when the scenario completes
curl -sk -X POST https://polaris-dev.xyz/api/replay/record/finalize \
     -H 'Content-Type: application/json' -d '{"cleanup":true}'

# 4. Snapshot the post-build /workspace into the assets dir
docker exec polaris-ws-<hash> sh -c \
  'cd /workspace && tar --exclude=./node_modules --exclude=./dist \
                       --exclude=./.git --exclude=./.codex \
                       --exclude=./.playwright-mcp \
                       -czf /tmp/ws.tar.gz .'
docker cp polaris-ws-<hash>:/tmp/ws.tar.gz \
          tests/fixtures/replay/assets/<scenario>-workspace.tar.gz

# 5. Revert the env, restart
sed -i '/^POLARIS_RECORD=/d' .env
docker compose -f compose.dev.yaml up -d --force-recreate api worker
```

The fixture audit (`scripts/replay_audit.py <fixture>`) sanity-checks
coverage; run it after every recording.

## Replaying

```bash
# 1. Point the worker at your local recording
echo 'POLARIS_REPLAY=/home/sun/projects/polaris-project/tests/fixtures/replay/raw/<scenario>.json.gz' >> .env
docker compose -f compose.dev.yaml up -d --force-recreate api worker

# 2. Run the e2e
POLARIS_E2E_REPLAY=1 pnpm --filter @polaris/web exec \
  playwright test replay-<scenario>

# 3. Revert
sed -i '/^POLARIS_REPLAY=/d' .env
docker compose -f compose.dev.yaml up -d --force-recreate api worker
```

CI defaults to no `POLARIS_E2E_REPLAY` → the replay tests skip.
Add the env var to trigger them only when fixtures are pre-staged
(e.g. by a CI step that pulls them from internal storage).
