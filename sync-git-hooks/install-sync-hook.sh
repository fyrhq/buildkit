#!/bin/sh
set -eu

fail() { printf '[install] %s\n' "$*" >&2; exit 1; }

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    printf 'Usage: %s <repository-path>\n' "$0" >&2
    exit 2
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
for hook in pre-receive post-receive; do
    [ -f "$source_dir/$hook" ] && [ -x "$source_dir/$hook" ] ||
        fail "missing or non-executable hook: $source_dir/$hook"
done

cd -- "$1" || fail "cannot open repository: $1"
git rev-parse --git-dir >/dev/null 2>&1 || fail 'target is not a Git repository'

if configured_path=$(git config --get core.hooksPath); then
    if [ "$configured_path" = "$source_dir" ]; then
        printf '[install] hooks already configured: %s\n' "$source_dir"
        exit 0
    fi
    fail "core.hooksPath is already configured: $configured_path; decide how to integrate existing hooks before installing"
else
    status=$?
    [ "$status" -eq 1 ] || fail 'cannot read core.hooksPath configuration'
fi

# Changing hooksPath would disable all existing hooks, not only receive hooks.
hooks_dir=$(git rev-parse --path-format=absolute --git-path hooks)
for existing in "$hooks_dir"/*; do
    case "$existing" in *.sample) continue ;; esac
    if [ -f "$existing" ] && [ -x "$existing" ]; then
        fail "existing executable hook: $existing; decide how to integrate existing hooks before installing"
    fi
done

git config --local core.hooksPath "$source_dir"
printf '[install] hooks configured: %s\n' "$source_dir"
