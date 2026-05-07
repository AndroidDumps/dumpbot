# systemd units

Run dumpyarabot under systemd so that `git pull && systemctl restart dumpyarabot.target`
reloads the bot and worker together.

## Layout

- `dumpyarabot-bot.service` — runs `python -m dumpyarabot` (the Telegram bot).
- `dumpyarabot-worker.service` — runs `run_arq_worker.py` (the ARQ job worker).
- `dumpyarabot.target` — convenience grouping; restart this to reload both.

The units assume:

- Code lives at `/var/lib/jenkins/dumpbot`.
- The runtime user is `jenkins`.
- A uv-managed venv exists at `/var/lib/jenkins/dumpbot/.venv` (`uv sync` creates it).
- A `.env` file in the working directory provides `TELEGRAM_BOT_TOKEN`,
  `DUMPER_TOKEN`, `REDIS_URL`, etc. (pydantic-settings reads it from cwd).

If any of those differ on your box, edit the units before installing.

## Install

```sh
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
journalctl -u dumpyarabot-bot.service -f
journalctl -u dumpyarabot-worker.service -f
```

## Multiple workers

The unit runs a single worker process. ARQ's `ARQ_MAX_JOBS=1` means each worker
handles one dump at a time, but you can run several workers in parallel by
copying the unit to `dumpyarabot-worker@.service` (a template) and starting
instances like `systemctl start dumpyarabot-worker@1.service`. Add those
instances to `dumpyarabot.target`'s `Wants=` list if you want them grouped.
