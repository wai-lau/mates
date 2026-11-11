import os, subprocess, pathlib, pytest
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
    counter = tmp_path / "count"
    r = mkrepo(tmp_path, f"# lockfiles: uv.lock\necho x >> {counter}\necho FOO=bar > $MATES_RUNTIME/env\n")
    d1 = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    d2 = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert d1 == d2 and counter.read_text().count("x") == 1

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
    seen = tmp_path / "seen"
    r = mkrepo(tmp_path, f"# lockfiles: uv.lock\n[ -L \"$MATES_RUNTIME\" ] && echo link > {seen}; [ -d \"$MATES_RUNTIME\" ] && echo dir >> {seen}\n")
    runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert seen.read_text() == "dir\n"

def test_runtime_planted_link_not_trusted(tmp_path, private_dir):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\necho FOO=mine > $MATES_RUNTIME/env\n")
    h = runtime.runtime_hash(r, head(r))
    other = tmp_path / "other"; other.mkdir(); (other / "env").write_text("PATH=/evil\n")
    (private_dir / "rt" / "repo").mkdir(parents=True)
    (private_dir / "rt" / "repo" / h).symlink_to(other)
    d = runtime.ensure_runtime(r, head(r), root=private_dir / "rt")
    assert not d.is_symlink() and runtime.runtime_env(d) == ["FOO=mine"]
