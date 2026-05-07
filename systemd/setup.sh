#!/usr/bin/env bash
# Install dumpyarabot's user-mode systemd units. Idempotent: re-running is safe.
#
# Run as the user the services should run as (the units hard-code paths
# under /var/lib/jenkins, so `jenkins` is expected):
#     ./systemd/setup.sh
#
# The script links the units from this checkout into the user's
# `~/.config/systemd/user/` so a `git pull` updates them in place.
#
# To install for a different user (e.g. while testing), edit the units
# to match that user's paths and pass `--force` to bypass the user check.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UNITS=(
    dumpyarabot-redis.service
    dumpyarabot-bot.service
    dumpyarabot-worker.service
    dumpyarabot.target
)

EXPECTED_USER="jenkins"
REDIS_DATA_DIR="/var/lib/jenkins/dumpbot-redis"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

force=0
if [ "${1:-}" = "--force" ]; then
    force=1
fi

ok()   { printf '  \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m !\033[0m %s\n' "$*" >&2; }
err()  { printf '  \033[31mxx\033[0m %s\n' "$*" >&2; }

# 0. Sanity: the units pin /var/lib/jenkins paths, so installing as anyone
# else is almost certainly wrong. Allow override for deliberate test setups.
if [ "$USER" != "$EXPECTED_USER" ] && [ "$force" -ne 1 ]; then
    err "running as '$USER' but the units expect '$EXPECTED_USER'"
    err "  unit files hard-code /var/lib/jenkins/... paths; installing them"
    err "  under another user will produce broken services on next start."
    err "  Either run this script as '$EXPECTED_USER' (e.g. 'sudo -iu $EXPECTED_USER $SCRIPT_DIR/setup.sh')"
    err "  or edit the unit files to match your layout and re-run with --force."
    exit 1
fi

# 1. Linger — the only step that needs root, and only if not already on.
if loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes$'; then
    ok "linger enabled for $USER"
else
    warn "linger NOT enabled for $USER"
    warn "  one-time root step:  sudo loginctl enable-linger $USER"
    warn "  (without this, units stop on logout and don't survive reboot)"
fi

# 2. Redis data directory. /var/lib/jenkins is jenkins-owned in the canonical
# layout, so plain mkdir works. If we're not jenkins or the dir lives elsewhere,
# point the user at install(1).
if [ -d "$REDIS_DATA_DIR" ]; then
    if [ -w "$REDIS_DATA_DIR" ]; then
        ok "redis data dir exists: $REDIS_DATA_DIR"
    else
        warn "redis data dir exists but is NOT writable by $USER: $REDIS_DATA_DIR"
        warn "  fix ownership:  sudo chown -R $USER:$USER $REDIS_DATA_DIR"
    fi
elif mkdir -p "$REDIS_DATA_DIR" 2>/dev/null; then
    ok "created redis data dir: $REDIS_DATA_DIR"
else
    warn "could not create $REDIS_DATA_DIR (not writable as $USER)"
    warn "  one-time root step:  sudo install -d -o $USER -g $USER -m 0750 $REDIS_DATA_DIR"
fi

# 3. User unit directory + symlinks back to this checkout.
mkdir -p "$USER_UNIT_DIR"
for unit in "${UNITS[@]}"; do
    src="$SCRIPT_DIR/$unit"
    dst="$USER_UNIT_DIR/$unit"
    if [ ! -e "$src" ]; then
        warn "missing unit in checkout: $src"
        continue
    fi
    ln -sfn "$src" "$dst"
done
ok "linked units into $USER_UNIT_DIR"

# 4. Reload manager and enable the target. Both are idempotent.
systemctl --user daemon-reload
systemctl --user enable --now dumpyarabot.target
ok "dumpyarabot.target enabled and started"

# 5. Status snapshot.
echo
systemctl --user --no-pager --output=short list-units 'dumpyarabot*' || true
