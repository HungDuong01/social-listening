# syntax=docker/dockerfile:1
#
# Base image choice - deliberate, not default:
#
# Microsoft's official Playwright-for-Python image, not a slim Python image
# plus a manual `playwright install --with-deps chromium`. This project has
# already hit real bugs from a mismatch between the pip-installed Playwright
# version and the Chromium build actually on disk (see README, section on
# the "Executable doesn't exist" / chromium_headless_shell failures hit
# during development). This base image bakes Chromium in at the exact
# revision its matching pip package expects, which removes that entire
# failure class outright. The trade-off, accepted here, is a larger image:
# Firefox and WebKit are baked in alongside Chromium and go unused.
ARG PLAYWRIGHT_VERSION=1.62.0

FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependency layer - cached independently of application source changes.
# ---------------------------------------------------------------------------
FROM base AS deps
ARG PLAYWRIGHT_VERSION

# Pinned first, exactly matching the base image tag above. requirements.txt
# deliberately leaves Playwright unpinned (`playwright>=1.48`) so a plain
# `pip install` on a developer's own machine always resolves to a version
# whose matching browser build Playwright then fetches itself. Installing
# the exact pin here first means the later `pip install -r requirements.txt`
# finds it already satisfied and leaves it untouched - no risk of it
# drifting from the browser build this image was made for, and no need to
# touch the shared requirements.txt (which the host dev setup also uses).
RUN pip install "playwright==${PLAYWRIGHT_VERSION}"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime image.
# ---------------------------------------------------------------------------
FROM deps AS runtime

# System packages needed only at runtime, kept in their own layer so a
# requirements.txt change never forces re-downloading these:
#
#   xvfb        Virtual X display. TikTok's search page answers a headless
#               Chromium's User-Agent with an error page (measured against
#               the live site - see README); the crawler therefore always
#               launches Chromium "headed" for TikTok, which needs a real X
#               server even though nobody is watching.
#   x11vnc      Exposes that virtual display over VNC so a human can drive
#               the interactive login flow: Threads/Instagram/Facebook
#               logins open a real, visible browser window for the person
#               to sign into by hand - there is no other way to do that
#               inside a container with no physical screen.
#   novnc,
#   websockify  Serve a VNC-in-the-browser client so viewing that window
#               needs only a web browser, not a dedicated VNC viewer.
#   fluxbox     Minimal window manager. Without one, Chromium's window has
#               no decorations and some dialogs behave oddly under Xvfb.
#   tini        Real PID 1. Chromium is well known to leave zombie
#               processes behind when nothing reaps them, which a plain
#               shell script as PID 1 does not do.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        fluxbox \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root. Browsers baked into this base image live under /ms-playwright
# and are shipped world-writable by Microsoft's own build (their Dockerfile
# runs `chmod -R 777` on every browser directory), so a fresh unprivileged
# user needs no extra chown there to launch them.
RUN useradd --create-home --uid 10001 --shell /bin/bash appuser

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser web ./web
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser run.py ./run.py
COPY --chown=appuser:appuser docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# data/ sessions/ exports/ profiles/ are also created at startup by
# Settings.model_post_init(), but creating them here first - with the right
# owner - matters: Docker seeds a fresh named volume from whatever already
# exists at its mount point in the image, so a root-owned directory here
# would make the volume root-owned too, and appuser would fail to write to
# it the moment the container actually starts.
RUN mkdir -p data sessions exports profiles \
    && chown -R appuser:appuser /app

USER appuser

ENV SC_DATA_DIR=/app/data \
    SC_SESSIONS_DIR=/app/sessions \
    SC_EXPORTS_DIR=/app/exports \
    SC_PROFILES_DIR=/app/profiles \
    DISPLAY=:99

EXPOSE 8000 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
