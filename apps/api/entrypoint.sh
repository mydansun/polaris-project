#!/bin/sh
# Re-register editable installs against the LIVE bind-mount path before
# running the api.  Build-time `uv pip install -e` records the path
# /app/apps/api as the editable target, but at runtime the source lives
# under ${POLARIS_HOST_REPO_ROOT} (bind-mounted at the same absolute
# path inside the container as it has on the host).  Re-running install
# with --no-deps + same venv just rewrites the .pth file to the correct
# location — fast, idempotent, no network round-trip.
set -e

: "${POLARIS_HOST_REPO_ROOT:?must be set by compose.dev.yaml}"

cd "${POLARIS_HOST_REPO_ROOT}"
uv pip install --quiet --no-deps \
    -e packages/agent-core \
    -e apps/api

exec "$@"
