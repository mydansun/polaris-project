# Polaris chromium-vnc runtime — linuxserver/chromium + a CDP reverse proxy.
#
# Why a custom image: Chromium M111+ silently ignores
# `--remote-debugging-address=0.0.0.0` and binds CDP to 127.0.0.1 only,
# and ALSO rejects inbound requests whose Host header isn't `localhost`
# or a literal IP (DNS-rebinding protection).  That breaks our design —
# the workspace container runs @playwright/mcp and needs to reach this
# container's CDP at http://chromium-vnc:9223.
#
# Fix: drop an nginx server block at /etc/nginx/conf.d/cdp-proxy.conf.
# The linuxserver/chromium image already runs nginx (for its VNC web UI
# on 3000/3001) with `include /etc/nginx/conf.d/*.conf`, so our config
# gets picked up on container start — no init-hook or separate daemon
# required.  The proxy rewrites the Host header to `localhost:9222` on
# the way upstream, which satisfies Chromium's rebinding check.

FROM lscr.io/linuxserver/chromium:latest

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY cdp-proxy.conf /etc/nginx/conf.d/cdp-proxy.conf
