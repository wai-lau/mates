import os, subprocess, pathlib, pytest
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

def test_ensure_runtime_builds_once(tmp_path):
    counter = tmp_path / "count"
    r = mkrepo(tmp_path, f"# lockfiles: uv.lock\necho x >> {counter}\necho FOO=bar > $MATES_RUNTIME/env\n")
    d1 = runtime.ensure_runtime(r, head(r), root=tmp_path / "rt")
    d2 = runtime.ensure_runtime(r, head(r), root=tmp_path / "rt")
    assert d1 == d2 and counter.read_text().count("x") == 1

def test_runtime_env_loaded(tmp_path):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\necho FOO=bar > $MATES_RUNTIME/env\n")
    d = runtime.ensure_runtime(r, head(r), root=tmp_path / "rt")
    assert runtime.runtime_env(d) == ["FOO=bar"]

def test_failed_build_leaves_nothing(tmp_path):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\nexit 3\n")
    with pytest.raises(RuntimeError): runtime.ensure_runtime(r, head(r), root=tmp_path / "rt")
    assert not any((tmp_path / "rt").rglob("env"))

@pytest.mark.parametrize("p", ["~/.ssh/id_ed25519", "x/.env", "a.pem", "~/.secrets", "~/.claude/x", "y/.credentials.json"])
def test_inject_refuses_secret_paths(tmp_path, p):
    path = pathlib.Path(os.path.expanduser(p)) if p.startswith("~") else tmp_path / p
    with pytest.raises(ValueError): runtime.check_inject(path)

def test_inject_refuses_token_content(tmp_path):
    f = tmp_path / "cfg.txt"; f.write_text("GH_TOKEN=ghp_" + "a" * 36 + "\n")
    with pytest.raises(ValueError): runtime.check_inject(f)

def test_inject_refuses_token_in_dir(tmp_path):
    d = tmp_path / "tools"; d.mkdir(); (d / "ok.txt").write_text("fine"); (d / "k").write_text("api_key = abcdefgh12345")
    with pytest.raises(ValueError): runtime.check_inject(d)

def test_stage_injects_records_hash(tmp_path):
    src = tmp_path / "tool.sh"; src.write_text("echo hi\n")
    task = tmp_path / "task"; (task / "ctl").mkdir(parents=True)
    rec = runtime.stage_injects(task, [f"{src}:bin/tool.sh"])
    assert rec[0]["dest"] == "bin/tool.sh" and len(rec[0]["sha256"]) == 64
    staged = task / "ctl/inject/0"
    assert staged.read_text() == "echo hi\n"
    assert staged.stat().st_mode & 0o222 == 0

def test_runtime_not_group_writable(tmp_path):
    r = mkrepo(tmp_path, "# lockfiles: uv.lock\nmkdir -p $MATES_RUNTIME/bin; echo x > $MATES_RUNTIME/bin/t; chmod 775 $MATES_RUNTIME/bin\n")
    d = runtime.ensure_runtime(r, head(r), root=tmp_path / "rt")
    assert all(f.stat().st_mode & 0o022 == 0 for f in [d, *d.rglob("*")])
