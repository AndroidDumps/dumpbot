# systemd units (system mode)

Run dumpyarabot's redis, bot, and worker as **system-scope** systemd units
(in `/etc/systemd/system/`, owned by root) running as `User=jenkins`. System
scope puts them in `/system.slice/` instead of `user@.service`'s cgroup,
which keeps the firmware worker out of `systemd-oomd`'s PSI kill path (see
[Why system-scope](#why-system-scope) below).

## Layout

- `dumpyarabot-redis.service` — runs `redis-server /var/lib/jenkins/dumpbot/redis.conf`
  on port 34790, loopback-only, persisting RDB to `/var/lib/jenkins/dumpbot-redis`.
- `dumpyarabot-bot.service` — runs `python -m dumpyarabot` (the Telegram bot).
- `dumpyarabot-worker@.service` — templated unit; `run_arq_worker.py worker_%i`
  (the ARQ job worker). `setup.sh` enables instances `@1` and `@2` by default.
- `dumpyarabot.target` — convenience grouping; restart this to reload everything.

The units assume:

- Code lives at `/var/lib/jenkins/dumpbot` (jenkins-owned).
- A uv-managed venv exists at `/var/lib/jenkins/dumpbot/.venv` (`uv sync` creates it).
- A `.env` file in the working directory provides `TELEGRAM_BOT_TOKEN`,
  `DUMPER_TOKEN`, `REDIS_URL`, etc. (pydantic-settings reads it from cwd).
  `REDIS_URL` should point at `redis://127.0.0.1:34790/0` to match `redis.conf`.
- A redis data directory at `/var/lib/jenkins/dumpbot-redis` (created by `setup.sh`).

If any of those differ, edit the units / `redis.conf` before installing.

## Install / re-install

Everything that touches `/etc/systemd/system/` needs root. There's a script:

```sh
sudo /var/lib/jenkins/dumpbot/systemd/setup.sh
```

`setup.sh` is idempotent. It:
1. Creates `/var/lib/jenkins/dumpbot-redis` (jenkins-owned) if missing.
2. Migrates from a previous user-scope install (stops / disables / removes the
   legacy units in `~jenkins/.config/systemd/user/`).
3. Symlinks the four unit files into `/etc/systemd/system/`.
4. `daemon-reload`s.
5. `enable --now`s `dumpyarabot.target` plus each `dumpyarabot-worker@N.service`.

Re-run it after pulling unit-file additions or renames. For content-only edits
to existing units, `sudo systemctl daemon-reload && sudo systemctl restart dumpyarabot.target`
is enough.

## Reload after `git pull`

Code changes (anything outside `systemd/`):

```sh
# As jenkins (read-only git operation, file ownership stays correct):
sudo -u jenkins git -C /var/lib/jenkins/dumpbot pull --ff-only
sudo systemctl restart dumpyarabot.target
```

Unit-file changes also need `sudo systemctl daemon-reload` first.

The worker carries `TimeoutStopSec=7260` (2h + 1min). On SIGTERM the worker
propagates a `CancelledError` into the running job, which lets `process_utils`
clean up its subprocess tree (`_kill_process_tree` SIGKILLs the session) and
report a graceful failure to Telegram instead of being SIGKILLed at an
arbitrary point. The 7260s ceiling matches arq's `job_timeout = 7200s` plus a
minute of grace, so even a job that stalls during cancellation shutdown gets
a fair window. The bot/redis use 30s.

## Logs

System-scope units use plain `journalctl`, not `--user`:

```sh
sudo journalctl -u dumpyarabot-redis.service -f
sudo journalctl -u dumpyarabot-bot.service -f
sudo journalctl -u 'dumpyarabot-worker@*.service' -f
```

## Multiple workers

The worker is a systemd template (`dumpyarabot-worker@.service`). Each instance
is one process running `run_arq_worker.py worker_<N>`. ARQ's `ARQ_MAX_JOBS=1`
means each instance handles one dump at a time, so to run N dumps concurrently
you enable N instances.

Default install enables `@1` and `@2`. To change the count:

1. Edit `WORKER_INSTANCES=(1 2 ...)` in `setup.sh`.
2. Update `dumpyarabot.target`'s `Wants=`/`After=` to list each instance.
3. Re-run `setup.sh`.

Both instances share the same `WORK_DIR_BASE` (typically `/dumps3/bot/`) —
per-job subdirs are uniquely named (`dump_<jobid>_<rand>/`) so they don't
collide. On startup each worker sweeps stale `dump_*` dirs from a previous
crash; see `on_startup` in `dumpyarabot/arq_config.py`.

## Why system-scope

Earlier we ran these as user-scope units under the jenkins user manager
(`systemctl --user`). That broke under load: the Ubuntu 22.04 distro defaults
in `/usr/lib/systemd/system/user@.service.d/10-oomd-user-service-defaults.conf`
set `ManagedOOMMemoryPressure=kill` with a 50% PSI threshold on every
per-user manager. Whenever the jenkins user manager's slice hit 50% PSI for
20s — which a legitimate large-RAM firmware extraction reliably produces —
systemd-oomd SIGKILLed every process in the user manager's heaviest
descendant cgroup. That was always the worker.

On Ubuntu 22.04 / systemd 249 there is *no working off-switch* for this:

- `ManagedOOMPreference=omit` on the worker requires systemd >= 250.
- `ManagedOOMMemoryPressure=auto` on `user@.service` crashes the user
  manager with `status=219/CGROUP` on startup.
- `ManagedOOMMemoryPressureLimit=…` on `user@.service` (raising the
  threshold) also crashes it with `status=219/CGROUP`.
- `ManagedOOMMemoryPressureLimit=100%` on the *worker* unit is ignored
  because oomd doesn't monitor the worker; it monitors the ancestor
  `user@.service` and selects the worker as the heaviest victim.

System-scope sidesteps all of it. The worker lives in `/system.slice/`,
which is not in oomd's monitored-cgroups list at all. The kernel OOM killer
(with proper badness scoring) remains as the real safety net, which is
what we actually want.
