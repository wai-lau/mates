#!/usr/bin/env bash
# mates sandbox: run a command as the Unix user `claude` inside bubblewrap.
#
#   sandbox.sh --task-dir /srv/mates/tasks/<id> [--setenv K=V]... \
#              [--runtime <dir>] [--inject <ctl-path>:<dest>]... -- <cmd...>
#
# The task dir holds wt/ (the worktree, mounted at /work, the only writable
# repo path), ctl/ (read-only control files: check.sh, settings.json,
# CLAUDE.md, inject/), state/ (the worker's Claude config dir, writable) and
# sock/ (the forwarded Ollama socket). No home directory is mounted, the
# network namespace has only loopback, and the environment starts empty.
# If bwrap cannot start, the command does not run: there is no fallback.
#
#   --no-bwrap-for-test  keep the user switch, drop bwrap: ONLY for
#                        tests/sandbox_escape.sh, to show what the user
#                        switch holds on its own.
set -euo pipefail

die() { echo "sandbox: $*" >&2; exit 125; }

TASK= NOBWRAP=0 RUNTIME=
SETENV=() INJECT=()
while [ $# -gt 0 ]; do
  case $1 in
    --task-dir) TASK=$2; shift 2 ;;
    --setenv) SETENV+=("$2"); shift 2 ;;
    --runtime) RUNTIME=$2; shift 2 ;;
    --inject) INJECT+=("$2"); shift 2 ;;
    --no-bwrap-for-test) NOBWRAP=1; shift ;;
    --) shift; break ;;
    *) die "unknown argument $1" ;;
  esac
done
[ $# -ge 1 ] || die "no command"
[ -n "$TASK" ] || die "--task-dir required"
TASK=$(realpath -e "$TASK") || die "no task dir"
case $TASK in /srv/mates/tasks/?*) ;; *) die "task dir must be under /srv/mates/tasks/" ;; esac
[ -d "$TASK/wt" ] || die "no worktree at $TASK/wt"
for d in ctl state sock; do [ -d "$TASK/$d" ] || die "missing $TASK/$d"; done

BASE_ENV=(PATH=/usr/bin:/bin HOME=/home/mates TERM="${TERM:-xterm-256color}" LANG=C.UTF-8 USER=claude PYTHONDONTWRITEBYTECODE=1)
RT_ENV=()
if [ -n "$RUNTIME" ]; then
  RUNTIME=$(realpath -e "$RUNTIME") || die "no runtime dir"
  case $RUNTIME in /srv/mates/runtimes/?*) ;; *) die "runtime must be under /srv/mates/runtimes/" ;; esac
  if [ -f "$RUNTIME/env" ]; then
    while IFS= read -r line; do
      [[ $line =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] && RT_ENV+=("$line")
    done < "$RUNTIME/env"
  fi
fi

if [ "$NOBWRAP" = 1 ]; then
  exec sudo -n -u claude env -i "${BASE_ENV[@]/HOME=\/home\/mates/HOME=/tmp}" "${SETENV[@]}" \
    bash -c 'cd "$1" && shift && exec "$@"' bash "$TASK/wt" "$@"
fi

A=(--unshare-all --die-with-parent --new-session --clearenv
   --ro-bind /usr /usr
   --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64
   --proc /proc --dev /dev --tmpfs /tmp
   --tmpfs /home --dir /home/mates
   --ro-bind /srv/mates/bin /srv/mates/bin
   --bind "$TASK/wt" /work
   --bind "$TASK/state" /home/mates/.claude-cfg
   --bind "$TASK/sock" /run/mates)
for f in passwd group nsswitch.conf ld.so.cache ld.so.conf ld.so.conf.d ssl ca-certificates alternatives localtime; do
  [ -e "/etc/$f" ] && A+=(--ro-bind "/etc/$f" "/etc/$f")
done
[ -f "$TASK/ctl/check.sh" ] && A+=(--ro-bind "$TASK/ctl/check.sh" /work/.mates/check.sh)
for f in settings.json CLAUDE.md; do
  [ -f "$TASK/ctl/$f" ] && A+=(--ro-bind "$TASK/ctl/$f" "/home/mates/.claude-cfg/$f")
done
for extra in goal.md change.diff; do
  [ -f "$TASK/ctl/$extra" ] && A+=(--ro-bind "$TASK/ctl/$extra" "/work/.mates/$extra")
done
[ -n "$RUNTIME" ] && A+=(--ro-bind "$RUNTIME" "$RUNTIME")
for inj in "${INJECT[@]}"; do
  src=${inj%%:*} dest=${inj#*:}
  src=$(realpath -e "$src") || die "no inject source $src"
  case $src in "$TASK"/ctl/inject/?*) ;; *) die "inject source must be staged under ctl/inject/" ;; esac
  [[ $dest = /* ]] || dest=/work/$dest
  A+=(--ro-bind "$src" "$dest")
done
for kv in "${BASE_ENV[@]}" "${RT_ENV[@]}" "${SETENV[@]}"; do
  A+=(--setenv "${kv%%=*}" "${kv#*=}")
done
A+=(--chdir /work)

# Inside: start the Ollama forwarder (the only route out), then the command.
exec sudo -n -u claude /usr/bin/bwrap "${A[@]}" -- /bin/bash -c '
  python3 /srv/mates/bin/fwd.py in --sock /run/mates/ollama.sock --listen 127.0.0.1:11434 &
  # Audit: sample this namespace'"'"'s TCP sockets every 5 s (pilot PII bar: zero non-loopback peers).
  (while :; do ss -tnH >> /run/mates/net.log 2>/dev/null; sleep 5; done) &
  for _ in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/11434) 2>/dev/null && break; sleep 0.1; done
  umask 002; "$@"; rc=$?; kill %1 %2 2>/dev/null; exit $rc' bash "$@"
