"""Brief loading, guard flags on a worker's diff, and authorship redaction.

Guard flags tell the lead when it must read the full diff: a file the brief
did not name, a test touched, a test skipped, an assertion removed, anything
under .mates/, or text that names a model (which would also leak authorship
to the blind verifier).
"""
import json
import re

TEST_FILE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")
SKIP = re.compile(r"pytest\.mark\.(skip|xfail)|unittest\.skip|\bxit\(|\.only\(|\bt\.Skip\(|\bskip\(")
ASSERT = re.compile(r"\bassert|expect\(|\.should\b|assertEqual|assertTrue")
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
    """Yield (path, added_lines, removed_lines) per file in a unified diff."""
    path, minus, added, removed = None, None, [], []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if path:
                yield path, added, removed
            path, minus, added, removed = None, None, [], []
        elif line.startswith("--- "):
            minus = line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            p = line[4:]
            path = minus if p == "/dev/null" else p.removeprefix("b/")
        elif path and line.startswith("+"):
            added.append(line[1:])
        elif path and line.startswith("-"):
            removed.append(line[1:])
    if path:
        yield path, added, removed


def guard_flags(diff_text: str, brief_files: list[str]) -> list[str]:
    named = set(brief_files)
    flags = []
    for path, added, removed in _files(diff_text):
        if path.startswith(".mates/") or path == ".mates":
            flags.append(f"mates-dir:{path}")
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
