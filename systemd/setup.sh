#!/usr/bin/env bash
# Install dumpyarabot's system-mode systemd units. Idempotent.
#
# Must be run as root (units live in /etc/systemd/system/, services run
# as the `jenkins` user via User=jenkins in the unit). Typical:
#
#     sudo /var/lib/jenkins/dumpbot/systemd/setup.sh
#
# Units are SYMLINKED from this checkout into /etc/systemd/system/ so
# `git pull` updates them in place. Re-run setup.sh after a pull only
# if the set of unit files changed; for content-only edits, a plain
# `sudo systemctl daemon-reload && sudo systemctl restart dumpyarabot.target`
# is enough.
#
# Why system-scope: the worker process gets SIGKILLed by systemd-oomd
# when run under the jenkins user manager (user@130.service has a 50%
# PSI kill threshold from Ubuntu 22.04's distro defaults, and oomd has
# no working off-switch on systemd 249 — every supported override
# crashes user@.service with status=219/CGROUP). Moving units to the
# system slice puts the worker outside oomd's PSI monitoring entirely;
# the kernel OOM killer remains as the real safety net.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UNITS=(
    dumpyarabot-redis.service
    dumpyarabot-bot.service
    dumpyarabot-worker@.service
    dumpyarabot.target
)

# Templated worker instances to enable. Add e.g. `3 4` here (and update
# dumpyarabot.target's Wants=) to run more workers in parallel.
WORKER_INSTANCES=(1 2)

RUN_USER="jenkins"
REDIS_DATA_DIR="/var/lib/jenkins/dumpbot-redis"
SYSTEM_UNIT_DIR="/etc/systemd/system"

ok()   { printf '  \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m !\033[0m %s\n' "$*" >&2; }
err()  { printf '  \033[31mxx\033[0m %s\n' "$*" >&2; }

# 0. Must be root.
if [ "$(id -u)" -ne 0 ]; then
    err "must run as root (the units live in $SYSTEM_UNIT_DIR/)"
    err "  try:  sudo $0"
    exit 1
fi

# 1. Redis data directory — owned by the run user.
if [ ! -d "$REDIS_DATA_DIR" ]; then
    install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$REDIS_DATA_DIR"
    ok "created redis data dir: $REDIS_DATA_DIR"
elif [ "$(stat -c '%U' "$REDIS_DATA_DIR")" != "$RUN_USER" ]; then
    chown -R "$RUN_USER:$RUN_USER" "$REDIS_DATA_DIR"
    ok "chowned $REDIS_DATA_DIR to $RUN_USER"
else
    ok "redis data dir exists and is owned by $RUN_USER: $REDIS_DATA_DIR"
fi

# 2. Migrate from a previous USER-scope install, if present. The old
# install left symlinks in ~jenkins/.config/systemd/user/ and used
# `systemctl --user`. Stop & remove that whole arrangement before
# installing the system units, to avoid two managers fighting.
USER_UNIT_DIR="$(eval echo "~$RUN_USER")/.config/systemd/user"
if [ -d "$USER_UNIT_DIR" ] && \
   ls -1 "$USER_UNIT_DIR"/dumpyarabot* >/dev/null 2>&1; then
    warn "found legacy user-scope install in $USER_UNIT_DIR"
    user_uid="$(id -u "$RUN_USER")"
    user_runtime="/run/user/$user_uid"
    user_sc() {
        sudo -u "$RUN_USER" XDG_RUNTIME_DIR="$user_runtime" systemctl --user "$@"
    }
    user_sc stop dumpyarabot.target 'dumpyarabot-*' 2>/dev/null || true
    user_sc disable dumpyarabot.target 'dumpyarabot-worker@*.service' 2>/dev/null || true
    rm -f "$USER_UNIT_DIR"/dumpyarabot* \
          "$USER_UNIT_DIR"/default.target.wants/dumpyarabot* 2>/dev/null || true
    # Without this, the user manager keeps stale unit metadata until it
    # exits — confusing if you go looking for them via --user later.
    user_sc daemon-reload 2>/dev/null || true
    ok "removed legacy user-scope units"
fi

# 3. Symlink units from the checkout into /etc/systemd/system/.
for unit in "${UNITS[@]}"; do
    src="$SCRIPT_DIR/$unit"
    dst="$SYSTEM_UNIT_DIR/$unit"
    if [ ! -e "$src" ]; then
        warn "missing unit in checkout: $src"
        continue
    fi
    ln -sfn "$src" "$dst"
done
ok "linked units into $SYSTEM_UNIT_DIR"

# 4. Reload and enable everything.
systemctl daemon-reload

worker_units=()
for i in "${WORKER_INSTANCES[@]}"; do
    worker_units+=("dumpyarabot-worker@${i}.service")
done
systemctl enable --now dumpyarabot.target "${worker_units[@]}"
ok "dumpyarabot.target + ${#worker_units[@]} worker instance(s) enabled and started"

# 5. Status snapshot.
echo
systemctl --no-pager --output=short list-units 'dumpyarabot*' || true
