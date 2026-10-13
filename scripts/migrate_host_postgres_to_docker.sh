#!/usr/bin/env bash
# Copy the data from the host-installed PostgreSQL (used when running
# `python run.py` directly) into the containerized "db" service defined in
# docker-compose.yml. One-time, run from Git Bash at the project root:
#
#   scripts/migrate_host_postgres_to_docker.sh
#
# Both databases already share the same schema/role name ("social_crawler"
# owning a "social_crawler" database, "crawler" schema) by construction -
# see scripts/bootstrap_postgres.sql for the host side and
# docker/postgres-init.sql for the container side - so a plain data-only
# dump/restore is enough; no column mapping or transformation is needed.
set -euo pipefail

# --- host (source) -----------------------------------------------------
readonly HOST_PG_BIN="/c/Program Files/PostgreSQL/18/bin"
readonly HOST_HOST=127.0.0.1
readonly HOST_PORT=5433
readonly HOST_DB=social_crawler
readonly HOST_USER=social_crawler

# --- container (destination) -------------------------------------------
readonly DOCKER_HOST=127.0.0.1
readonly DOCKER_DB=social_crawler
readonly DOCKER_USER=social_crawler

log() { printf '[migrate] %s\n' "$*" >&2; }

require_file() {
    if [ ! -x "$1" ]; then
        log "FATAL: expected an executable at $1 - adjust HOST_PG_BIN for this machine"
        exit 1
    fi
}

require_env_file() {
    if [ ! -f .env.docker ]; then
        log "FATAL: .env.docker not found - copy .env.docker.example to .env.docker first"
        exit 1
    fi
}

read_env_var() {
    # .env.docker is a plain KEY=VALUE file; no need for a parsing library.
    grep -E "^${1}=" .env.docker | tail -1 | cut -d= -f2-
}

main() {
    require_env_file
    require_file "${HOST_PG_BIN}/pg_dump.exe"
    require_file "${HOST_PG_BIN}/psql.exe"

    local docker_port
    docker_port="$(read_env_var POSTGRES_HOST_PORT)"
    docker_port="${docker_port:-5434}"

    local docker_password
    docker_password="$(read_env_var POSTGRES_APP_PASSWORD)"
    if [ -z "${docker_password}" ]; then
        log "FATAL: POSTGRES_APP_PASSWORD not set in .env.docker"
        exit 1
    fi

    log "checking the container database is reachable on port ${docker_port}"
    if ! PGPASSWORD="${docker_password}" "${HOST_PG_BIN}/psql.exe" \
        -h "${DOCKER_HOST}" -p "${docker_port}" -U "${DOCKER_USER}" -d "${DOCKER_DB}" \
        -c "select 1" >/dev/null 2>&1; then
        log "FATAL: could not reach the container's PostgreSQL on ${DOCKER_HOST}:${docker_port}."
        log "Is 'docker compose --env-file .env.docker up -d db' running?"
        exit 1
    fi

    log "reading current row counts on the container (before)"
    PGPASSWORD="${docker_password}" "${HOST_PG_BIN}/psql.exe" \
        -h "${DOCKER_HOST}" -p "${docker_port}" -U "${DOCKER_USER}" -d "${DOCKER_DB}" \
        -c "select 'posts' t, count(*) from crawler.posts
            union all select 'comments', count(*) from crawler.comments"

    log "dumping data-only from the host database (schema already exists on both sides)"
    # --data-only: the target schema is created by docker/postgres-init.sql +
    # the app's own startup migration, not by this dump - dumping schema too
    # would just fight with CREATE TABLE IF NOT EXISTS-style idempotency the
    # app already relies on elsewhere.
    # --disable-triggers: safe here because FK constraints are re-validated
    # implicitly by insert order; the app never defines triggers of its own.
    PGPASSWORD_UNUSED= "${HOST_PG_BIN}/pg_dump.exe" \
        -h "${HOST_HOST}" -p "${HOST_PORT}" -U "${HOST_USER}" -d "${HOST_DB}" \
        --data-only --disable-triggers --no-owner --no-privileges \
        --schema=crawler \
        --format=custom --file=/tmp/social_crawler_data.dump

    log "restoring into the container database"
    PGPASSWORD="${docker_password}" "${HOST_PG_BIN}/pg_restore.exe" \
        -h "${DOCKER_HOST}" -p "${docker_port}" -U "${DOCKER_USER}" -d "${DOCKER_DB}" \
        --data-only --disable-triggers --no-owner --no-privileges \
        /tmp/social_crawler_data.dump

    rm -f /tmp/social_crawler_data.dump

    log "row counts on the container (after)"
    PGPASSWORD="${docker_password}" "${HOST_PG_BIN}/psql.exe" \
        -h "${DOCKER_HOST}" -p "${docker_port}" -U "${DOCKER_USER}" -d "${DOCKER_DB}" \
        -c "select 'posts' t, count(*) from crawler.posts
            union all select 'comments', count(*) from crawler.comments"

    log "done"
}

main "$@"
