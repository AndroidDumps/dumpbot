# systemd units (user mode)

Run dumpyarabot's redis, bot, and worker under the **jenkins user's** systemd
manager so `git pull && systemctl --user restart dumpyarabot.target` reloads
everything with no sudo.

## Layout

- `dumpyarabot-redis.service` — runs `redis-server /var/lib/jenkins/dumpbot/redis.conf`
  on port 34790, loopback-only, persisting RDB to `/var/lib/jenkins/dumpbot-redis`.
- `dumpyarabot-bot.service` — runs `python -m dumpyarabot` (the Telegram bot).
- `dumpyarabot-worker.service` — runs `run_arq_worker.py` (the ARQ job worker).
- `dumpyarabot.target` — convenience grouping; restart this to reload all three.

The units assume:

- Code lives at `/var/lib/jenkins/dumpbot`.
- A uv-managed venv exists at `/var/lib/jenkins/dumpbot/.venv` (`uv sync` creates it).
- A `.env` file in the working directory provides `TELEGRAM_BOT_TOKEN`,
  `DUMPER_TOKEN`, `REDIS_URL`, etc. (pydantic-settings reads it from cwd).
  `REDIS_URL` should point at `redis://127.0.0.1:34790/0` to match `redis.conf`.
- A redis data directory at `/var/lib/jenkins/dumpbot-redis`.

If any of those differ, edit the units / `redis.conf` before installing.

## One-time setup

The only step that needs root is enabling lingering, so the jenkins user
manager runs without a login session and survives reboot:

```sh
sudo loginctl enable-linger jenkins
```

Everything else runs as the `jenkins` user — there's a script for it:

```sh
sudo -iu jenkins /var/lib/jenkins/dumpbot/systemd/setup.sh
```

`setup.sh` is idempotent: it creates `/var/lib/jenkins/dumpbot-redis`, symlinks
the four unit files into `~jenkins/.config/systemd/user/`, runs
`systemctl --user daemon-reload`, and `enable --now`s the target. Re-run it
after pulling unit-file changes from the repo.

## Reload after `git pull`

From a jenkins shell, no sudo:

```sh
git -C /var/lib/jenkins/dumpbot pull --ff-only
systemctl --user daemon-reload   # only needed if a unit file actually changed
systemctl --user restart dumpyarabot.target
```

The worker carries `TimeoutStopSec=7260` (2h + 1min). On SIGTERM the worker
propagates a `CancelledError` into the running job, which lets `process_utils`
clean up its subprocess tree (`_kill_process_tree` SIGKILLs the session) and
report a graceful failure to Telegram instead of being SIGKILLed at an
arbitrary point. The 7260s ceiling matches arq's `job_timeout = 7200s` plus a
minute of grace, so even a job that stalls during cancellation shutdown gets
a fair window. The bot/redis use 30s.

## Logs

```sh
journalctl --user -u dumpyarabot-redis.service -f
journalctl --user -u dumpyarabot-bot.service -f
journalctl --user -u dumpyarabot-worker.service -f
```

## Multiple workers

The unit runs a single worker process. ARQ's `ARQ_MAX_JOBS=1` means each worker
handles one dump at a time, but you can run several workers in parallel by
copying the unit to `dumpyarabot-worker@.service` (a template) and starting
instances like `systemctl --user start dumpyarabot-worker@1.service`. Add those
instances to `dumpyarabot.target`'s `Wants=` list if you want them grouped.
