import json, pathlib, subprocess, pytest
import tasks, runtime

STUBS = pathlib.Path(__file__).parent / "stubs"

def git(r, *a): return subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True).stdout.strip()

@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"; (r / "src").mkdir(parents=True); (r / "tests").mkdir()
    git(tmp_path, "init", "-q", str(r)); git(r, "config", "user.email", "w@w"); git(r, "config", "user.name", "w")
    (r / "src/calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "tests/test_calc.py").write_text("import sys; sys.path.insert(0, 'src')\nimport calc\ndef test_x(): assert calc.add(1, 1) == 1 - 1\n")
    git(r, "add", "-A"); git(r, "commit", "-qm", "base")
    (r / "src/calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "tests/test_calc.py").write_text("import sys; sys.path.insert(0, 'src')\nimport calc\ndef test_x(): assert calc.add(1, 1) == 2\n")
    git(r, "commit", "-qam", "fix add to add")
    (r / "README").write_text("docs\n"); git(r, "add", "-A"); git(r, "commit", "-qm", "docs only")
    return r

def test_candidates_need_src_and_test(repo):
    c = tasks.candidates(repo, since="10 years ago")
    assert [x["msg"] for x in c] == ["fix add to add"]
    assert c[0]["src_files"] == ["src/calc.py"] and c[0]["test_files"] == ["tests/test_calc.py"]

def test_candidates_skip_browser_tests(repo):
    (repo / "src/calc.py").write_text("def add(a, b):\n    return b + a\n")
    (repo / "tests/test_ui_browser.py").write_text("def test_b(): pass\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "browser only")
    assert "browser only" not in [x["msg"] for x in tasks.candidates(repo, since="10 years ago")]

def test_runtime_script_for_uv(repo):
    (repo / "uv.lock").write_text("x"); (repo / "pyproject.toml").write_text("[project]\nname='p'\n")
    s = tasks.runtime_script_for(repo)
    assert s.startswith("# lockfiles: uv.lock pyproject.toml") and "uv sync --frozen" in s and "--all-extras" not in s

def test_runtime_script_for_plain_gets_pytest_venv(repo):
    s = tasks.runtime_script_for(repo)
    assert "python3 -m venv" in s and "pytest" in s

def test_uv_runtime_keeps_python_inside_runtime(repo):
    (repo / "uv.lock").write_text("x")
    s = tasks.runtime_script_for(repo)
    assert "UV_PYTHON_INSTALL_DIR=$MATES_RUNTIME/python" in s and "pytest" in s

def test_check_for():
    assert tasks.check_for(["tests/test_a.py"]) == "python3 -m pytest -q -p no:cacheprovider tests/test_a.py"

def test_task_valid_requires_red_base_green_target(repo, tmp_path):
    c = tasks.candidates(repo, since="10 years ago")[0]
    kw = dict(sandbox=str(STUBS / "sandbox_stub.sh"), tasks_root=tmp_path / "t")
    ok, why, base = tasks.validate(repo, c, "python3 -m pytest -q -p no:cacheprovider tests/test_calc.py", **kw)
    assert ok, why
    # the task base is the parent plus the target's tests: old source, new tests
    assert "a - b" in git(repo, "show", f"{base}:src/calc.py") and "== 2" in git(repo, "show", f"{base}:tests/test_calc.py")
    ok, why, _ = tasks.validate(repo, c, "true", **kw)
    assert not ok and "green on base" in why

def test_write_brief_uses_test_diff_not_source(repo, tmp_path):
    c = tasks.candidates(repo, since="10 years ago")[0]
    seen = tmp_path / "prompt.txt"
    stub = tmp_path / "brief_stub.sh"
    stub.write_text(f'#!/usr/bin/env bash\ncat > {seen}\n'
                    'echo \'[{"type": "result", "result": "{\\"goal\\": \\"make add sum\\", \\"files\\": [\\"x\\"], \\"constraints\\": \\"none\\"}"}]\'\n')
    stub.chmod(0o755)
    b = tasks.write_brief(repo, c, cmd=[str(stub)])
    assert b == {"goal": "make add sum", "files": ["src/calc.py"], "constraints": "none"}
    p = seen.read_text()
    assert "== 2" in p and "a + b" not in p

def test_make_base_survives_stale_scratch(repo, tmp_path):
    c = tasks.candidates(repo, since="10 years ago")[0]
    stale = tmp_path / "t" / f"mkbase-{repo.name}-{c['commit'][:10]}"
    stale.mkdir(parents=True); (stale / "junk").write_text("x")
    base = tasks.make_base(repo, c, tmp_path / "t")
    assert "== 2" in git(repo, "show", f"{base}:tests/test_calc.py")

def test_build_drops_candidate_on_error(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "validate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    lines = []
    kept = tasks.build([str(repo)], tmp_path / "out", want=1, sandbox="x", tasks_root=tmp_path / "t", log=lines.append)
    assert kept == [] and any("error: boom" in l for l in lines)
