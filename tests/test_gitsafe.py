import os
import shutil
import subprocess

import pytest

import gitsafe
import session


def _git(cwd, *args, **kw):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, **kw).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    session.ensure_worktree(root, 1)
    return root


@pytest.fixture
def calls(monkeypatch):
    seen = []
    real = subprocess.run

    def run(argv, **kw):
        seen.append((argv, kw))
        return real(argv, **kw)
    monkeypatch.setattr(gitsafe.subprocess, "run", run)
    return seen


def test_git_safe_env_and_flags(repo, calls, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/somewhere/else")  # an inherited redirect is dropped
    wt = repo / ".claude" / "worktrees" / "mates-qwen-1"
    common = repo / ".git"
    r = gitsafe.git_safe(repo, 1, "diff", "--stat", "HEAD")
    assert r.returncode == 0
    argv, kw = calls[-1]
    assert argv[:6] == ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "diff"]
    assert argv[6:8] == ["--no-ext-diff", "--no-textconv"] and argv[8:] == ["--stat", "HEAD"]
    env = kw["env"]
    assert env["GIT_DIR"] == str(common / "worktrees" / "mates-qwen-1")
    assert env["GIT_COMMON_DIR"] == str(common)
    assert env["GIT_WORK_TREE"] == str(wt)
    assert env["GIT_INDEX_FILE"].endswith("/index") and str(wt) not in env["GIT_INDEX_FILE"]
    assert kw["cwd"] == wt and kw["text"] is True
    for sub in ("log", "show"):
        gitsafe.git_safe(repo, 1, sub, "-1", "HEAD")
        assert calls[-1][0][5:8] == [sub, "--no-ext-diff", "--no-textconv"]
    gitsafe.git_safe(repo, 1, "status", "--porcelain")
    assert "--no-ext-diff" not in calls[-1][0]


def test_git_safe_temp_index_is_removed(repo, calls):
    gitsafe.git_safe(repo, 1, "read-tree", "HEAD")
    idx = calls[-1][1]["env"]["GIT_INDEX_FILE"]
    assert not os.path.exists(idx)


def test_git_safe_caller_index(repo, tmp_path, calls):
    idx = tmp_path / "my-index"
    gitsafe.git_safe(repo, 1, "read-tree", "HEAD", index=idx)
    assert calls[-1][1]["env"]["GIT_INDEX_FILE"] == str(idx) and idx.exists()


def test_git_safe_raises_on_failure(repo):
    with pytest.raises(gitsafe.GitError, match="single revision"):
        gitsafe.git_safe(repo, 1, "rev-parse", "--verify", "no-such-ref^{commit}")
    assert gitsafe.git_safe(repo, 1, "rev-parse", "--verify", "-q", "nope", check=False).returncode != 0


def test_git_safe_needs_a_worktree_entry(repo):
    with pytest.raises(gitsafe.GitError, match="no git worktree entry"):
        gitsafe.git_safe(repo, 2, "status")


def test_git_lead_flags(repo, calls):
    gitsafe.git_lead(repo, "diff", "HEAD")
    argv, kw = calls[-1]
    assert argv == ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "diff",
                    "--no-ext-diff", "--no-textconv", "HEAD"]
    assert kw["cwd"] == repo and not any(k.startswith("GIT_DIR") for k in kw["env"])


def test_git_safe_ignores_redirected_commondir(repo, tmp_path):
    """Qwen's admin `commondir` (read-only in a mates pane; written here as if it got
    through) points at a copy of the git dir whose config runs a filter: plain git in the
    worktree would run it as Wai; git_safe does not."""
    wt = repo / ".claude" / "worktrees" / "mates-qwen-1"
    admin = repo / ".git" / "worktrees" / "mates-qwen-1"
    fake = tmp_path / "fake-git"
    shutil.copytree(repo / ".git", fake)
    canary = tmp_path / "canary"
    with open(fake / "config", "a") as f:
        f.write(f'[filter "evil"]\n\tclean = "touch {canary}; cat"\n')
    (admin / "commondir").write_text(f"{fake}\n")
    (wt / ".gitattributes").write_text("* filter=evil\n")
    (wt / "a.txt").write_text("changed\n")

    # The attack is real: plain git found through the worktree runs the planted filter.
    probe = tmp_path / "probe-index"
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_INDEX_FILE": str(probe)})
    assert canary.exists()
    canary.unlink()

    gitsafe.git_safe(repo, 1, "add", "-A", "--", ".")
    gitsafe.git_safe(repo, 1, "diff", "HEAD")
    assert not canary.exists()
