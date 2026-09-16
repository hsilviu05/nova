#!/bin/sh
# Dumps the database on a fixed interval and prunes old dumps.
#
# One compressed pg_dump per run, named by UTC timestamp, into /backups.
# Custom format (-Fc) so a restore can be selective and parallel. The
# prune is by age and runs after each successful dump, so a dump that
# fails never deletes anything.
set -eu

keep_days="${NOVA_BACKUP_KEEP_DAYS:-14}"
interval="${NOVA_BACKUP_INTERVAL_SECONDS:-86400}"
dir="${NOVA_BACKUP_DIR:-/backups}"

while :; do
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    target="${dir}/nova-${stamp}.dump"
    if pg_dump --format=custom --compress=6 --file="${target}.partial" && mv "${target}.partial" "${target}"; then
        echo "backup ok ${target} $(stat -c %s "${target}") bytes"
        find "${dir}" -name 'nova-*.dump' -mtime "+${keep_days}" -delete
    else
        echo "backup FAILED at ${stamp}" >&2
        rm -f "${target}.partial"
    fi
    sleep "${interval}"
done
