#!/usr/bin/env bash
set -ex

# If argument was given then format only that file, else format entire dumpyarabot
if [[ -z "$path" ]]; then
    path="dumpyarabot"
fi

# Format
autoflake --remove-all-unused-imports --recursive --remove-unused-variables --in-place ${path} --exclude=__init__.py
black ${path}
isort ${path}
