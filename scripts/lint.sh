#!/usr/bin/env bash

set -ex

# If argument was given then lint only that file, else lint entire dumpyarabot
if [[ -z "$path" ]]; then
    path="dumpyarabot"
fi

# Lint
mypy $path
black $path --check
isort --check-only $path
ruff $path
