"""Brief loading, guard flags on a worker's diff, and authorship redaction.

Guard flags tell the lead when it must read the full diff: a file the brief
did not name, a test touched, a test skipped, an assertion removed, anything
under .mates/, text that names a model (authorship), a hooks or `.git*` file (git could run
it as Wai), a manifest or lockfile a `mates runtime` build reads (in the build
sandbox, with registry-only network), a file that runs or instructs a lead outside the sandbox once merged
(`owner-side`), a link, or an exec bit. `needs_ack` names the flags merge refuses
until the lead acknowledges their kind.
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
# Files that run as Wai, or instruct a lead, outside the sandbox once merged (security audit
# 2026-11-13, finding 4): Claude Code settings, commands and instructions; direnv; build and
# test entry points; CI; editor tasks; package-manager configs.
OWNER_SIDE = re.compile(r"""^(?:.*/)?(?:
    \.claude/.* | \.mcp\.json | CLAUDE(?:\.local)?\.md | AGENTS\.md
  | \.envrc
  | (?:GNU)?[Mm]akefile | [Jj]ustfile | Taskfile\.ya?ml
  | conftest\.py | setup\.py | noxfile\.py | tox\.ini | \.pre-commit-config\.yaml
  | \.github/.* | \.gitlab-ci\.yml | \.circleci/.* | \.vscode/.* | \.idea/.*
  | \.npmrc | \.yarnrc(?:\.yml)? | \.pypirc | pip\.conf | uv\.toml
)$""", re.X)
INDEX_MODE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+ (\d{6})$")
LINK_MODE, EXEC_MODE = "120000", "100755"
# Flag kinds merge refuses until `--ack` names them: each runs something as Wai. A
# `.gitignore` keeps its hook-path flag (it is a `.git*` file) but runs nothing.
ACK_KINDS = ("owner-side", "symlink", "exec-bit", "hook-path", "runtime-input")
# An added line that quotes or credits the owner: the lead checks it against the brief (a Qwen
# invented an owner quote and "owner-approved" relays in an archaeology entry, 2026-11-12).
OWNER_WORD = re.compile(r"\b(wai|owner|approved|approval|decided)\b", re.I)
QUOTED = re.compile(r"\"[^\"]+\"|'[^']+'")
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


_ESC = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _unquote(p: str) -> str:
    """Undo git's C-style quoting of a path (`"b/caf\\303\\251"` -> `b/café`)."""
    if len(p) < 2 or not (p.startswith('"') and p.endswith('"')):
        return p
    out, i, body = bytearray(), 0, p[1:-1]
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt, oct3 = body[i + 1], body[i + 1:i + 4]
            if len(oct3) == 3 and all(ch in "01234567" for ch in oct3):
                out.append(int(oct3, 8) & 0xFF)
                i += 4
                continue
            out += _ESC.get(nxt, nxt).encode()
            i += 2
            continue
        out += c.encode()
        i += 1
    return out.decode("utf-8", "replace")


def _files(diff_text: str):
    """Yield (path, added_lines, removed_lines, new_modes) per file in a unified diff:
    new_modes holds the modes of the file's `new file mode` / `new mode` / `index a..b <mode>` header lines,
    read only before its first hunk, like the paths. The path
    comes from the `diff --git` line (so a binary file counts too), then from the
    `---`/`+++` headers, which are read only before the file's first hunk: a hunk
    line such as `+++ b/x` (an added `++ b/x`) cannot rename the file."""
    path, minus, added, removed, in_hunk, modes = None, None, [], [], False, set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if path:
                yield path, added, removed, modes
            path, minus, added, removed, in_hunk, modes = None, None, [], [], False, set()
            rest = line[len("diff --git "):]
            if rest.endswith('"'):  # git quotes a name with odd characters: "b/..."
                start = rest.rfind(' "b/')
                path = _unquote(rest[start + 1:])[2:] if start >= 0 else None
            else:
                a, sep, b = rest.partition(" b/")
                path = b if sep else None
        elif not in_hunk and line.startswith(("new file mode ", "new mode ")):
            modes.add(line.rsplit(" ", 1)[1])
        elif not in_hunk and (im := INDEX_MODE.match(line)):
            modes.add(im.group(1))  # git writes the mode here, and only here, when it did not change
        elif not in_hunk and line.startswith("--- "):
            minus = _unquote(line[4:]).removeprefix("a/")
        elif not in_hunk and line.startswith("+++ "):
            p = _unquote(line[4:])
            path = minus if p == "/dev/null" else p.removeprefix("b/")
        elif line.startswith("@@"):
            in_hunk = True
        elif path and in_hunk and line.startswith("+"):
            added.append(line[1:])
        elif path and in_hunk and line.startswith("-"):
            removed.append(line[1:])
    if path:
        yield path, added, removed, modes


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


def owner_quote_lines(diff_text: str) -> list[tuple[str, str]]:
    """(path, added line) for each added line that raised an owner-quote flag."""
    return [(path, l) for path, added, _, _ in _files(diff_text) for l in added
            if OWNER_WORD.search(l) and QUOTED.search(l)]


def guard_flags(diff_text: str, brief_files: list[str], hooks_path=None, runtime_files=(),
                binary_paths=()) -> list[str]:
    named = set(brief_files)
    hook_dirs = _hook_dirs(hooks_path)
    runtime = set(runtime_files)
    binary = set(binary_paths)
    flags = []
    for path, added, removed, modes in _files(diff_text):
        if path.startswith(".mates/") or path == ".mates":
            flags.append(f"mates-dir:{path}")
        if any(_under(path, d) for d in hook_dirs) or posixpath.basename(path).startswith((".git", ".lfsconfig")):
            flags.append(f"hook-path:{path}")
        if OWNER_SIDE.match(path):
            flags.append(f"owner-side:{path}")
        if LINK_MODE in modes:
            flags.append(f"symlink:{path}")
        if EXEC_MODE in modes:
            flags.append(f"exec-bit:{path}")
        if path in binary:
            flags.append(f"binary:{path}")
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
        if any(OWNER_WORD.search(l) and QUOTED.search(l) for l in added):
            flags.append(f"owner-quote:{path}")
        if any(AUTHORSHIP.search(l) for l in added):
            flags.append(f"authorship:{path}")
    return flags


def needs_ack(flags) -> list[str]:
    """The flags merge refuses until `--ack` names their kind."""
    return [f for f in flags if f.split(":", 1)[0] in ACK_KINDS
            and not (f.startswith("hook-path:") and posixpath.basename(f.split(":", 1)[1]) == ".gitignore")]


def redact_authorship(diff_text: str) -> str:
    return AUTHORSHIP.sub("[redacted]", diff_text)
