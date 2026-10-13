#!/usr/bin/env bash
#
# Entrypoint for the app container.
#
# Two things need a real X server here, not just "headless=True":
#
#   - TikTok's search page answers a headless Chromium's User-Agent with an
#     error page instead of results (measured against the live site - see
#     README). Every TikTok crawl therefore launches Chromium "headed",
#     whether or not anyone is watching.
#   - The interactive login flow (Threads/Instagram/Facebook) deliberately
#     opens a real, visible browser window for a human to sign into by
#     hand.
#
# Xvfb supplies that X server. x11vnc + noVNC make its display reachable
# from an ordinary web browser outside the container, since there is no
# physical screen attached to look at otherwise. This script brings all of
# that up, then hands off to the application itself, which becomes tini's
# direct child (started via `exec`, so it receives SIGTERM directly on
# `docker stop` and can run its own graceful shutdown - closing Playwright
# browsers cleanly matters here, since an unclean kill can leave lock files
# behind in the persistent browser-profile volume).

set -euo pipefail

readonly DISPLAY_NUM="${DISPLAY#:}"
readonly DISPLAY_RESOLUTION="1440x900x24"
readonly VNC_PORT=5900
readonly NOVNC_PORT=6080
readonly VNC_PASSWD_FILE="/tmp/.vncpasswd"

log() {
    printf '[entrypoint] %s\n' "$*" >&2
}

start_xvfb() {
    # A container restart (not a fresh container) keeps this same /tmp: the
    # previous Xvfb never gets a chance to remove its own lock file, since
    # tini only forwards the stop signal to its direct child (the Python
    # app), not to background jobs like Xvfb. Any lock/socket found here is
    # therefore always stale by the time this runs again - a live Xvfb
    # cannot coexist with this script starting a fresh one on the same
    # display. Measured live: without this cleanup, the new Xvfb dies
    # instantly ("Server is already active for display 99") while this
    # function's own readiness check falsely reports ready, because it only
    # checks for the lock file's existence - which the dead process already
    # satisfied.
    rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

    Xvfb "${DISPLAY}" -screen 0 "${DISPLAY_RESOLUTION}" -nolisten tcp &
    local xvfb_pid=$!

    # Xvfb forks and becomes ready asynchronously; waiting on its lock file
    # avoids a fixed sleep, which is either too slow (wastes it on every
    # single startup) or too fast (an intermittent race under load).
    local waited=0
    while [ ! -e "/tmp/.X${DISPLAY_NUM}-lock" ]; do
        if ! kill -0 "${xvfb_pid}" 2>/dev/null; then
            log "FATAL: Xvfb exited before becoming ready"
            exit 1
        fi
        if [ "${waited}" -ge 100 ]; then
            log "FATAL: Xvfb did not become ready within 10s"
            exit 1
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
    log "Xvfb ready on display ${DISPLAY} (${DISPLAY_RESOLUTION})"
}

start_window_manager() {
    # Best-effort: Chromium runs without one, just with undecorated windows.
    # Nothing downstream depends on this succeeding.
    fluxbox >/tmp/fluxbox.log 2>&1 &
}

start_vnc_stack() {
    # Fail loudly rather than silently starting an unauthenticated VNC
    # session: that window shows the platforms' real login forms, so an
    # open VNC port here is a credential leak waiting to happen the moment
    # this ever runs somewhere reachable by anyone else.
    : "${VNC_PASSWORD:?VNC_PASSWORD must be set in .env.docker - the login flow exposes a real browser window over VNC and must not run unauthenticated}"

    # -rfbauth over a passwd *file* rather than x11vnc's -passwd flag: the
    # latter puts the password on the process argv, visible to anyone else
    # on the same host via `ps aux`. Note VNC's own auth scheme is a dated,
    # weak DES-based obfuscation either way - adequate for keeping other
    # local processes and casual network scanners out, not a substitute for
    # keeping this port off the open internet (see README's Docker section).
    x11vnc -storepasswd "${VNC_PASSWORD}" "${VNC_PASSWD_FILE}" >/tmp/x11vnc-storepasswd.log 2>&1

    x11vnc -display "${DISPLAY}" -rfbport "${VNC_PORT}" \
        -rfbauth "${VNC_PASSWD_FILE}" \
        -forever -shared -quiet \
        >/tmp/x11vnc.log 2>&1 &
    log "x11vnc listening on 127.0.0.1:${VNC_PORT} (password required)"

    websockify --web=/usr/share/novnc "${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" \
        >/tmp/novnc.log 2>&1 &
    log "noVNC available at http://<host>:${NOVNC_PORT}/vnc.html"
}

apply_database_schema() {
    log "ensuring the PostgreSQL schema and indexes are up to date"
    python scripts/setup_postgres.py
}

start_xvfb
start_window_manager
start_vnc_stack
apply_database_schema

log "starting application"
exec python run.py
