#!/bin/sh
# See apps/api/entrypoint.sh for the rationale.  Worker depends on four
# packages, so we re-register all four against the bind-mount path.
set -e

: "${POLARIS_HOST_REPO_ROOT:?must be set by compose.dev.yaml}"

cd "${POLARIS_HOST_REPO_ROOT}"
uv pip install --quiet --no-deps \
    -e packages/agent-core \
    -e packages/design-intent \
    -e apps/api \
    -e apps/worker

exec "$@"
