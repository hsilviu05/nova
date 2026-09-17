#!/bin/bash
#
# Install NOVA as a macOS LaunchAgent, so the phone in the stand has a server
# to talk to after a reboot.
#
# A LaunchAgent rather than a LaunchDaemon: the API runs as you, reads a .env
# in your home directory, and needs no privilege. The trade is that it starts
# at *login* rather than at boot -- which is the right lifetime for a desk
# machine somebody signs into, and the wrong one for a headless server. If the
# Mac must serve without anyone logged in, this needs to become a daemon in
# /Library/LaunchDaemons, owned by root, with the paths made absolute.
#
#   scripts/install-launchagent.sh            # install and start
#   scripts/install-launchagent.sh --uninstall
#
# Logs: ~/Library/Logs/nova-api.log

set -euo pipefail

LABEL="com.hermeneanu.nova.api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/nova-api.log"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT/scripts/run-api.sh"

# `bootout` is the modern spelling and it fails when the job is not loaded,
# which is a normal state here rather than an error.
unload() {
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
    unload
    rm -f "$PLIST"
    echo "removed $LABEL"
    exit 0
fi

[[ -x "$RUNNER" ]] || chmod +x "$RUNNER"

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"

# Written with a heredoc rather than committed as a file because the paths are
# absolute and machine-specific, and a committed plist would carry one
# person's home directory into everybody else's checkout.
#
# The repository path here contains spaces and a typographic apostrophe, which
# is exactly why every value is its own <string> element: a plist array of
# arguments needs no quoting rules, unlike a shell command line.
cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$RUNNER</string>
    </array>

    <!-- Start at login, and again whenever it stops for any reason. The
         runner waits for Postgres and Redis itself, so an early start is
         patient rather than a crash loop. -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <!-- launchd throttles a job that exits within 10s of starting. Raised so
         a genuinely broken configuration backs off instead of rewriting the
         log ten times a minute. -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>WorkingDirectory</key>
    <string>$ROOT/services/api</string>

    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>

    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
PLIST_END

plutil -lint "$PLIST" >/dev/null

unload
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"

echo "installed $LABEL"
echo "  plist: $PLIST"
echo "  log:   $LOG"
echo
echo "  status:  launchctl print gui/$UID/$LABEL | head -20"
echo "  stop:    launchctl bootout gui/$UID/$LABEL"
echo "  restart: launchctl kickstart -k gui/$UID/$LABEL"
