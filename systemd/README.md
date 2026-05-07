# systemd units

Run dumpyarabot under systemd so that `git pull && systemctl restart dumpyarabot.target`
reloads the bot and worker together.

## Layout

- `dumpyarabot-redis.service` — runs `redis-server /var/lib/jenkins/dumpbot/redis.conf`
  on port 34790, loopback-only, persisting RDB to `/var/lib/jenkins/dumpbot-redis`.
- `dumpyarabot-bot.service` — runs `python -m dumpyarabot` (the Telegram bot).
- `dumpyarabot-worker.service` — runs `run_arq_worker.py` (the ARQ job worker).
- `dumpyarabot.target` — convenience grouping; restart this to reload all three.

The units assume:

- Code lives at `/var/lib/jenkins/dumpbot`.
- The runtime user is `jenkins`.
- A uv-managed venv exists at `/var/lib/jenkins/dumpbot/.venv` (`uv sync` creates it).
- A `.env` file in the working directory provides `TELEGRAM_BOT_TOKEN`,
  `DUMPER_TOKEN`, `REDIS_URL`, etc. (pydantic-settings reads it from cwd).
  `REDIS_URL` should point at `redis://127.0.0.1:34790/0` to match `redis.conf`.
- A redis data directory at `/var/lib/jenkins/dumpbot-redis`, owned by jenkins.

If any of those differ on your box, edit the units / `redis.conf` before installing.

## Install

```sh
# One-time: create the redis data directory
sudo install -d -o jenkins -g jenkins -m 0750 /var/lib/jenkins/dumpbot-redis

# Drop the units in place
sudo cp systemd/dumpyarabot-redis.service /etc/systemd/system/
sudo cp systemd/dumpyarabot-bot.service /etc/systemd/system/
sudo cp systemd/dumpyarabot-worker.service /etc/systemd/system/
sudo cp systemd/dumpyarabot.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dumpyarabot.target
```

## Reload after `git pull`

```sh
git -C /var/lib/jenkins/dumpbot pull --ff-only
sudo systemctl restart dumpyarabot.target
```

The worker carries `TimeoutStopSec=7260` (2h + 1min) so a SIGTERM during a
running dump waits for the job to finish or hit its `job_timeout` rather than
killing extraction mid-flight. The bot's stop timeout is 30s — it has no
long-lived in-process work.

## Logs

```sh
journalctl -u dumpyarabot-redis.service -f
journalctl -u dumpyarabot-bot.service -f
journalctl -u dumpyarabot-worker.service -f
```

## Multiple workers

The unit runs a single worker process. ARQ's `ARQ_MAX_JOBS=1` means each worker
handles one dump at a time, but you can run several workers in parallel by
copying the unit to `dumpyarabot-worker@.service` (a template) and starting
instances like `systemctl start dumpyarabot-worker@1.service`. Add those
instances to `dumpyarabot.target`'s `Wants=` list if you want them grouped.
