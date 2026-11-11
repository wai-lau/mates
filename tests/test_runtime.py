import os, pwd, shlex, shutil, socket, subprocess, pathlib, pytest
import fsutil
import runtime

def git(repo, *a): subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

def mkrepo(tmp, script=None, lock="v1"):
    r = tmp / "repo"; r.mkdir(); git(r, "init", "-q"); git(r, "config", "user.email", "t@t"); git(r, "config", "user.name", "t")
    (r / "uv.lock").write_text(lock)
    if script is not None:
        (r / ".mates").mkdir(); (r / ".mates/runtime.sh").write_text(script)
    git(r, "add", "-A"); git(r, "commit", "-qm", "c1")
    return r

def head(r): return subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

def test_no_runtime_script_is_none(tmp_path):
    r = mkrepo(tmp_path)
    assert runtime.runtime_hash(r, head(r)) is None

def test_hash_changes_with_lockfile(tmp_path):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\ntrue\n")
    h1 = runtime.runtime_hash(r, head(r))
    (r / "uv.lock").write_text("v2"); git(r, "commit", "-qam", "c2")
    assert runtime.runtime_hash(r, head(r)) != h1

def test_hash_ignores_worker_tree(tmp_path):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\ntrue\n"); base = head(r)
    h1 = runtime.runtime_hash(r, base)
    (r / ".mates/runtime.sh").write_text("rm -rf /\n")
    assert runtime.runtime_hash(r, base) == h1

def test_ensure_runtime_builds_once(tmp_path, private_dir):
    r = mkrepo(tmp_path, '# lockfiles: uv.lock\ndate +%s%N > "$MATES_RUNTIME/stamp"\n')
    d1 = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    stamp = (d1 / "stamp").read_text()
    d2 = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert d1 == d2 and (d2 / "stamp").read_text() == stamp

def test_runtime_env_loaded(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\necho FOO=bar > $MATES_RUNTIME/env\n")
    d = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert runtime.runtime_env(d) == ["FOO=bar"]

def test_failed_build_leaves_nothing(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\nexit 3\n")
    with pytest.raises(RuntimeError): runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert not any((private_dir / "rt").rglob("env"))

def test_runtime_not_group_writable(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\nmkdir -p $MATES_RUNTIME/bin; echo x > $MATES_RUNTIME/bin/t; chmod 775 $MATES_RUNTIME/bin\n")
    d = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert all(f.stat().st_mode & 0o022 == 0 for f in [d, *d.rglob("*")])

def test_runtime_refuses_shared_root(tmp_path):
    """Final review I4: a runtime folder another account can write (/srv/mates/runtimes is
    group emetcode; /tmp is everyone's) is never trusted or built in."""
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\ntrue\n")
    with pytest.raises(fsutil.UnsafeDir):
        runtime.ensure_runtime(r, head(r), root=tmp_path / "rt")

def test_runtime_default_root_is_private_state(tmp_path, private_dir, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(private_dir))
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\ntrue\n")
    d = runtime.ensure_runtime(r, head(r))
    assert d.parent.parent == private_dir / "mates" / "runtimes"
    assert all((p.stat().st_mode & 0o077) == 0 for p in (d.parent.parent, d.parent))

def test_runtime_build_goes_through_no_link(tmp_path, private_dir):
    r = mkrepo(tmp_path, '# lockfiles: uv.lock\n[ -L "$MATES_RUNTIME" ] && echo link > /tmp/seen; [ -d "$MATES_RUNTIME" ] && echo dir >> /tmp/seen; cp /tmp/seen "$MATES_RUNTIME/seen"\n')
    d = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert (d / "seen").read_text() == "dir\n"

def test_runtime_planted_link_not_trusted(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\necho FOO=mine > $MATES_RUNTIME/env\n")
    h = runtime.runtime_hash(r, head(r))
    other = tmp_path / "other"; other.mkdir(); (other / "env").write_text("PATH=/evil\n")
    (private_dir / "rt" / "repo").mkdir(parents=True)
    (private_dir / "rt" / "repo" / h).symlink_to(other)
    d = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert not d.is_symlink() and runtime.runtime_env(d) == ["FOO=mine"]


# --- the build runs in its own sandbox (Wai, 2026-11-11: the sandboxed-install option, then
# "PII and stuff must never enter the sandbox"). Internet only through qwen-relay egress (package
# registries on port 443), only the script and its named lockfiles from the base commit, in a
# throwaway copy, $MATES_RUNTIME at its final path; no home, no tokens, no host loopback.

def _built(tmp_path, private_dir, script):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\n" + script)
    return r, runtime.ensure_runtime(r, head(r), root=private_dir / "rt")

def test_build_runs_at_its_final_path(tmp_path, private_dir):
    _, d = _built(tmp_path, private_dir, 'echo "$MATES_RUNTIME" > "$MATES_RUNTIME/where"\n')
    assert (d / "where").read_text().strip() == str(d)

def test_build_sees_no_home_and_no_lead_checkout(tmp_path, private_dir):
    home = pwd.getpwuid(os.getuid()).pw_dir
    r = tmp_path / "repo"
    _, d = _built(tmp_path, private_dir, f'ls -A /home > "$MATES_RUNTIME/home"\n'
                  f'for p in {shlex.quote(home)} {shlex.quote(str(r))}; do [ -e "$p" ] && echo "$p" >> "$MATES_RUNTIME/seen"; done; true\n')
    assert (d / "home").read_text().split() == ["build"]
    assert not (d / "seen").exists()

def test_build_env_has_no_tokens(tmp_path, private_dir, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "sekrit-token")
    _, d = _built(tmp_path, private_dir, 'env > "$MATES_RUNTIME/envdump"\n')
    env = (d / "envdump").read_text()
    assert "sekrit-token" not in env and "GH_TOKEN" not in env
    assert "HTTPS_PROXY=http://127.0.0.1:" in env and f"MATES_RUNTIME={d}" in env

def test_build_reads_the_base_commit(tmp_path, private_dir):
    r = mkrepo(tmp_path, '# lockfiles: uv.lock\ncp uv.lock "$MATES_RUNTIME/lock"\n')
    (r / "uv.lock").write_text("dirty")  # not committed: the build must not see it
    d = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert (d / "lock").read_text() == "v1"

def test_build_sees_only_the_named_lockfiles(tmp_path, private_dir):
    r = tmp_path / "repo"; r.mkdir(); git(r, "init", "-q"); git(r, "config", "user.email", "t@t"); git(r, "config", "user.name", "t")
    (r / "uv.lock").write_text("v1"); (r / "notes.md").write_text("committed, not a lockfile")
    (r / ".gitignore").write_text(".env\n")
    (r / ".mates").mkdir(); (r / ".mates/runtime.sh").write_text('# lockfiles: uv.lock\nfind . -type f | sort > "$MATES_RUNTIME/files"\n')
    git(r, "add", "-A"); git(r, "commit", "-qm", "c1")
    (r / ".env").write_text("TOKEN=private"); (r / "draft.txt").write_text("untracked")
    d = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert (d / "files").read_text().split() == ["./uv.lock"]

def test_build_refuses_a_lockfile_outside_the_repo(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: ../uv.lock\ntrue\n")
    with pytest.raises(RuntimeError, match="lockfile"):
        runtime.ensure_runtime(r, head(r), root=private_dir / "rt")

def test_build_refuses_a_missing_lockfile(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock package-lock.json\ntrue\n")
    with pytest.raises(RuntimeError, match="package-lock.json"):
        runtime.ensure_runtime(r, head(r), root=private_dir / "rt")

def test_build_cannot_reach_host_loopback(tmp_path, private_dir):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0)); srv.listen()
    port = srv.getsockname()[1]
    try:
        r, d = _built(tmp_path, private_dir, f"""python3 - > "$MATES_RUNTIME/net" <<'PY'
import os, socket
try:
    socket.create_connection(("127.0.0.1", {port}), 2); print("direct reached")
except OSError:
    print("direct blocked")
s = socket.create_connection(("127.0.0.1", int(os.environ["HTTPS_PROXY"].rsplit(":", 1)[1])), 5)
s.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\\r\\n\\r\\n")
print("proxy", s.recv(64).split(b"\\r\\n")[0].decode())
PY
""")
    finally:
        srv.close()
    out = (d / "net").read_text()
    assert "direct blocked" in out and "proxy HTTP/1.1 403" in out
    assert "refused 127.0.0.1:443" in (d.parent / f"{d.name}.log").read_text()

def test_build_has_uv(tmp_path, private_dir):
    if not shutil.which("uv"):
        pytest.skip("no uv on this host")
    _, d = _built(tmp_path, private_dir, 'command -v uv > "$MATES_RUNTIME/uv"\n')
    assert (d / "uv").read_text().strip() == "/opt/mates/bin/uv"

def test_build_failure_shows_its_output(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\necho no-such-package >&2; exit 3\n")
    with pytest.raises(RuntimeError, match="no-such-package"):
        runtime.ensure_runtime(r, head(r), root=private_dir / "rt")

def test_build_refuses_without_bwrap(tmp_path, private_dir, monkeypatch):
    which = shutil.which
    monkeypatch.setattr(runtime.shutil, "which", lambda n, *a, **k: None if n == "bwrap" else which(n, *a, **k))
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\ntrue\n")
    with pytest.raises(RuntimeError, match="bwrap"):
        runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
