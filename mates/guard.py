"""Brief loading, guard flags on a worker's diff, and authorship redaction.

Guard flags tell the lead when it must read the full diff: a file the brief
did not name, a test touched, a test skipped, an assertion removed, anything
under .mates/, text that names a model (which would also leak authorship
to the blind verifier), a hooks or `.git*` file (git could run it as Wai), or
a manifest or lockfile the next launch rebuilds the runtime from (as Wai, with
network).
"""
import json
import posixpath
import re

TEST_FILE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")
SKIP = re.compile(r"pytest\.mark\.(skip|xfail)|unittest\.skip|\bxit\(|\.only\(|\bt\.Skip\(|\bskip\(")
ASSERT = re.compile(r"\bassert|expect\(|\.should\b|assertEqual|assertTrue")
HOOK_DIRS = (".githooks", ".husky")
MANIFEST = re.compile(r"^(pyproject\.toml|uv\.lock|requirements[^/]*\.txt|package\.json|package-lock\.json"
                      r"|Cargo\.toml|Cargo\.lock)$")
AUTHORSHIP = re.compile(r"\b(qwen|claude|anthropic|gpt|llm|language model|ai[- ]generated|as an ai)\b", re.I)


def load_brief(path) -> dict:
    with open(path) as f:
        b = json.load(f)
    for k in ("goal", "files", "constraints"):
        if k not in b:
            raise ValueError(f"brief missing {k!r}")
    if not str(b["goal"]).strip() or not b["files"]:
        raise ValueError("brief needs a goal and at least one file")
    return b


def _files(diff_text: str):
    """Yield (path, added_lines, removed_lines) per file in a unified diff. The path
    comes from the `diff --git` line (so a binary file counts too), then from the
    `---`/`+++` headers, which are read only before the file's first hunk: a hunk
    line such as `+++ b/x` (an added `++ b/x`) cannot rename the file."""
    path, minus, added, removed, in_hunk = None, None, [], [], False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if path:
                yield path, added, removed
            path, minus, added, removed, in_hunk = None, None, [], [], False
            a, sep, b = line[len("diff --git "):].partition(" b/")
            if sep:
                path = b
        elif not in_hunk and line.startswith("--- "):
            minus = line[4:].removeprefix("a/")
        elif not in_hunk and line.startswith("+++ "):
            p = line[4:]
            path = minus if p == "/dev/null" else p.removeprefix("b/")
        elif line.startswith("@@"):
            in_hunk = True
        elif path and in_hunk and line.startswith("+"):
            added.append(line[1:])
        elif path and in_hunk and line.startswith("-"):
            removed.append(line[1:])
    if path:
        yield path, added, removed


def _under(path, d):
    return path == d or path.startswith(d + "/")


def _hook_dirs(hooks_path):
    """The repo-relative hooks folders: .githooks/, .husky/ and core.hooksPath when it
    names a folder inside the repo."""
    out = list(HOOK_DIRS)
    if hooks_path and not posixpath.isabs(hooks_path):
        d = posixpath.normpath(hooks_path)
        if d not in (".", "") and not d.startswith("../"):
            out.append(d)
    return out


def guard_flags(diff_text: str, brief_files: list[str], hooks_path=None, runtime_files=()) -> list[str]:
    named = set(brief_files)
    hook_dirs = _hook_dirs(hooks_path)
    runtime = set(runtime_files)
    flags = []
    for path, added, removed in _files(diff_text):
        if path.startswith(".mates/") or path == ".mates":
            flags.append(f"mates-dir:{path}")
        if any(_under(path, d) for d in hook_dirs) or posixpath.basename(path).startswith(".git"):
            flags.append(f"hook-path:{path}")
        if path in runtime or MANIFEST.match(posixpath.basename(path)):
            flags.append(f"runtime-input:{path}")
        if path not in named:
            flags.append(f"unnamed-file:{path}")
        if TEST_FILE.search(path):
            flags.append(f"test-file:{path}")
            if any(SKIP.search(l) for l in added):
                flags.append(f"test-skipped:{path}")
            if any(ASSERT.search(l) for l in removed):
                flags.append(f"assertion-removed:{path}")
        if any(AUTHORSHIP.search(l) for l in added):
            flags.append(f"authorship:{path}")
    return flags


def redact_authorship(diff_text: str) -> str:
    return AUTHORSHIP.sub("[redacted]", diff_text)
