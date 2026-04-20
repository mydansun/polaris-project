#!/bin/sh
# Polaris workspace container entrypoint.
# 1. Starts supervisord (codex app-server + polaris-bg programs)
# 2. Execs the Theia IDE

set -e

if ! pgrep -x supervisord >/dev/null 2>&1; then
    supervisord -c /etc/supervisor/supervisord.conf 2>>/tmp/supervisord.boot.log || {
        echo "polaris-workspace: WARN failed to start supervisord (see /tmp/supervisord.boot.log)" >&2
    }
fi

exec node /app/src-gen/backend/main.js \
    --hostname 0.0.0.0 --port 3000 \
    /workspace "$@"
