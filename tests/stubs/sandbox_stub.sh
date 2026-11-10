#!/usr/bin/env bash
# Test stand-in for mates/sandbox.sh: same arguments, no bwrap, no user switch.
set -euo pipefail
TASK=
while [ $# -gt 0 ]; do case $1 in --task-dir) TASK=$2; shift 2;; --setenv|--runtime|--inject) shift 2;; --) shift; break;; *) shift;; esac; done
mkdir -p "$TASK/wt/.mates"
for f in check.sh goal.md change.diff; do [ -f "$TASK/ctl/$f" ] && cp "$TASK/ctl/$f" "$TASK/wt/.mates/$f"; done
cd "$TASK/wt"; PYTHONDONTWRITEBYTECODE=1 exec "$@"
