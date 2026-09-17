#!/bin/bash
#
# Run the NOVA API the way a desk terminal needs it: in the foreground, on
# every interface, with its dependencies confirmed first.
#
# This is what the LaunchAgent execs. It exists as a script rather than as a
# long ProgramArguments list because three things here are easy to get wrong
# and invisible when you do:
#
#   1. The working directory. `Settings.model_config` reads `env_file` as the
#      relative pair ("../../.env", ".env"), so the JWT secret is only found
#      when the process starts in services/api. Started anywhere else the API
#      comes up with no secret and refuses every request.
#
#   2. Dependency ordering. A LaunchAgent starts at login, which can be
#      before Postgres.app has opened its socket. Migrations would fail and
#      launchd would throttle the restart to once every ten seconds while the
#      log filled with connection errors. Waiting is cheaper than retrying.
#
#   3. PYTHONPATH. The editable install writes a .pth file, and macOS sets
#      UF_HIDDEN on it inside an iCloud-synced folder -- which Python 3.13+
#      then skips, so `import nova` fails. Setting the path explicitly costs
#      nothing and removes the failure mode.
#
# Usage: scripts/run-api.sh [--port 8000] [--host 0.0.0.0]

set -euo pipefail

HOST="0.0.0.0"
PORT="8000"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done

# Resolved from this script's own location, so the repository can be moved or
# cloned anywhere without editing the plist.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT/services/api"
VENV="$API/.venv"

if [[ ! -x "$VENV/bin/uvicorn" ]]; then
    echo "No virtualenv at $VENV. Create one:" >&2
    echo "  python3.12 -m venv '$VENV' && '$VENV/bin/pip' install -e '$API[dev]'" >&2
    exit 69
fi

if [[ ! -f "$ROOT/.env" && ! -f "$API/.env" ]]; then
    echo "No .env found. NOVA_JWT__SECRET_KEY has no default and the API will not start." >&2
    echo "  cp '$ROOT/.env.example' '$ROOT/.env'" >&2
    echo "  echo \"NOVA_JWT__SECRET_KEY=\$(openssl rand -base64 48 | tr -d '\\n')\" >> '$ROOT/.env'" >&2
    exit 78
fi

cd "$API"
export PYTHONPATH="$API/src"

# Homebrew and Postgres.app are not on a LaunchAgent's default PATH, which is
# /usr/bin:/bin:/usr/sbin:/sbin.
export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/Postgres.app/Contents/Versions/latest/bin:$PATH"

# -- wait for the dependencies -----------------------------------------------

# Read from the same configuration the app will read, rather than assuming
# localhost: a NOVA pointed at a Postgres on another machine should wait for
# that one.
read -r DB_HOST DB_PORT REDIS_HOST REDIS_PORT <<<"$(
    "$VENV/bin/python" - <<'PY'
from nova.core.config import get_settings

settings = get_settings()
print(settings.database.host, settings.database.port, settings.redis.host, settings.redis.port)
PY
)"

wait_for() {
    local label="$1" host="$2" port="$3" waited=0
    until "$VENV/bin/python" - "$host" "$port" <<'PY'
import socket, sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2):
        pass
except OSError:
    raise SystemExit(1)
PY
    do
        if (( waited == 0 )); then
            echo "waiting for $label at $host:$port ..."
        fi
        # No ceiling on purpose. A Mac that has just woken up may take a
        # while, and launchd's KeepAlive would restart this anyway -- looping
        # here keeps one log line instead of a restart storm.
        sleep 2
        waited=$((waited + 2))
    done
    (( waited > 0 )) && echo "$label ready after ${waited}s"
    return 0
}

wait_for postgres "$DB_HOST" "$DB_PORT"
wait_for redis "$REDIS_HOST" "$REDIS_PORT"

# -- migrate, then serve ------------------------------------------------------

# Run every time. The schema must never be a version behind the code that
# queries it, and an up-to-date database makes this a no-op in well under a
# second.
echo "applying migrations ..."
"$VENV/bin/alembic" upgrade head

echo "starting NOVA on $HOST:$PORT"
# exec, so launchd supervises uvicorn itself rather than this shell -- a
# stop or a crash is then seen directly instead of through a wrapper that
# would keep the job looking alive.
#
# No --reload: it watches the filesystem, and this tree is iCloud-synced.
exec "$VENV/bin/uvicorn" nova.main:create_app --factory --host "$HOST" --port "$PORT"
