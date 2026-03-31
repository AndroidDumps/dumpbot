# dumpbot

Telegram bot for queuing and processing firmware dumps with a separate ARQ worker.

## Link

https://t.me/dumpyarabot

## Setup

This bot now has two running parts:

- the Telegram bot process
- at least one ARQ worker process for dump jobs

### Requirements

- Python 3.10+
- Redis
- `uv`

### Install

```bash
uv sync
```

### Configure

Copy `.env.example` to `.env` and set the values you need:

```bash
TELEGRAM_BOT_TOKEN=...
ALLOWED_CHATS=[-1001234567890]
REQUEST_CHAT_ID=-1001234567890
REVIEW_CHAT_ID=-1001234567891
REDIS_URL=redis://localhost:6379/0
SUDO_USERS=[]
```

`REDIS_URL` is required because both the bot and the worker use Redis for queueing and state.

## Run

Start Redis first, then run the worker and bot in separate terminals.

### Start one worker

```bash
python run_arq_worker.py
```

You can also use the ARQ CLI:

```bash
arq worker_settings.WorkerSettings
```

### Start the bot

```bash
python -m dumpyarabot
```

## Commands

### `/dump [URL] [options]`

Queue a new firmware dump job.

Options:
- `a` use the alternative dumper
- `f` force a new dump even if one exists

Examples:
- `/dump https://example.com/firmware.zip`
- `/dump https://example.com/firmware.zip a`
- `/dump https://example.com/firmware.zip f`
- `/dump https://example.com/firmware.zip af`

### `/cancel [job_id]`

Cancel an active dump job.

Example:
- `/cancel 123`

### `/status`

Show queue or job status.
