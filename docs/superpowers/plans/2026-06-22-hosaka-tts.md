# hosaka TTS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, near-real-time (<1s to first audio) TTS tool with choosable/tunable voices — Kokoro presets + Chatterbox cloning live, Parler offline voice-baking — fronted by an HTTP server and a persistent REPL client.

**Architecture:** A FastAPI server holds two models resident in VRAM (Kokoro for fast presets, Chatterbox for cloning+tuning) and streams raw PCM. A thin REPL client auto-spawns the server, then pipes streamed audio into `pacat`. A separate, isolated venv runs Parler offline to "bake" text-described voices into seed WAVs that Chatterbox can then clone. The <1s lever is server-side sentence-fragment chunking, not model streaming flags.

**Tech Stack:** Python 3.12, PyTorch 2.9.1+cu128, FastAPI/Uvicorn, Kokoro-82M, Chatterbox (`davidbrowne17/chatterbox-streaming` fork), Parler-TTS Mini v1.1, PulseAudio `pacat` (WSLg), pytest.

## Global Constraints

Copied verbatim from the spec. Every task implicitly includes these.

- **Torch pin:** `torch==2.9.1+cu128` and `torchaudio==2.9.1+cu128`, installed from `--index-url https://download.pytorch.org/whl/cu128` FIRST, before any TTS package; TTS packages installed `--no-deps`; non-torch deps re-added after.
- **GPU truth:** never trust `torch.cuda.is_available()` alone — a real `(x @ x)` matmul on CUDA must succeed; device capability must be `(12, 0)`.
- **Audio:** install only `pulseaudio-utils` (client). NEVER `apt install pulseaudio` (daemon) — breaks WSLg. NEVER install Linux GPU drivers or `cuda`/`cuda-drivers` meta-packages inside WSL.
- **Engine:** use the ORIGINAL Chatterbox model (keeps `exaggeration`/`cfg_weight`/`temperature`), NOT Turbo.
- **Audio format everywhere:** raw PCM, 24 kHz, mono, float32 little-endian.
- **Concurrency:** single GPU, `asyncio.Semaphore(1)`; return `503` when busy.
- **Scope:** English only. Single user. No browser/MP3/CORS in v1. No pitch control.
- **Voice data:** user voices live in `~/.local/share/hosaka/voices/` (untracked); repo ships only sample seeds.
- **Style:** no Unicode emoji anywhere (code, comments, commits, docs); kaomoji or plain text only. Code snippets in fenced blocks.
- **Default voice/backend:** `kokoro` / `af_heart`.

---

## File Structure

```
~/src/hosaka/
  pyproject.toml
  .gitignore
  README.md
  hosaka/
    __init__.py
    config.py                # constants: SAMPLE_RATE, paths, defaults, LLM model
    chunking.py              # split_fragments()
    library.py               # VoiceLibrary, VoiceEntry
    schemas.py               # SpeechRequest, SpeechParams, VoiceInfo, clamp_params()
    audio.py                 # PacatPlayer
    server/
      __init__.py
      engines/
        __init__.py
        base.py              # Engine protocol, EngineRegistry
        kokoro_engine.py     # KokoroEngine
        chatterbox_engine.py # ChatterboxEngine
      app.py                 # create_app(): lifespan, /health, /v1/voices, /v1/audio/speech
    cli/
      __init__.py
      replcmd.py             # parse_line() -> ReplAction
      repl.py                # REPL client main
      bake.py                # Parler bake CLI (run via .venv-bake)
    sample_voices/           # a couple of shipped seed WAVs
  scripts/
    verify_gpu.py            # real-matmul GPU check
    benchmark_latency.py     # the <1s gate
  docs/
    API.md
  tests/
    test_chunking.py
    test_library.py
    test_replcmd.py
    test_schemas.py
    test_audio.py
    test_engines_gpu.py      # @pytest.mark.gpu
    test_server.py
```

---

### Task 1: Scaffold the hosaka repo

**Files:**
- Create: `~/src/hosaka/.gitignore`, `pyproject.toml`, `README.md`, `hosaka/__init__.py`, `hosaka/config.py`, `tests/__init__.py`, `conftest.py`
- Create empty package dirs with `__init__.py`: `hosaka/server/`, `hosaka/server/engines/`, `hosaka/cli/`

**Interfaces:**
- Produces: `hosaka.config` constants used by every later task.

- [ ] **Step 1: Create the repo and directory skeleton**

```bash
mkdir -p ~/src/hosaka/hosaka/server/engines \
         ~/src/hosaka/hosaka/cli \
         ~/src/hosaka/hosaka/sample_voices \
         ~/src/hosaka/scripts ~/src/hosaka/docs ~/src/hosaka/tests
cd ~/src/hosaka && git init -q
touch hosaka/__init__.py hosaka/server/__init__.py \
      hosaka/server/engines/__init__.py hosaka/cli/__init__.py \
      tests/__init__.py
```

- [ ] **Step 2: Write `.gitignore`**

```
.venv-server/
.venv-bake/
__pycache__/
*.pyc
.pytest_cache/
*.wav
!hosaka/sample_voices/*.wav
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[project]
name = "hosaka"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
markers = [
    "gpu: requires a CUDA GPU and downloaded models (deselect with -m 'not gpu')",
]
addopts = "-q"
```

- [ ] **Step 4: Write `hosaka/config.py`**

```python
from pathlib import Path

SAMPLE_RATE = 24000          # Hz, mono, float32 LE everywhere
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8123
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

DEFAULT_BACKEND = "kokoro"
DEFAULT_VOICE = "af_heart"

LLM_MODEL = "gpt-oss:20b"     # stopped on server start to free VRAM

VOICE_DIR = Path.home() / ".local" / "share" / "hosaka" / "voices"
MANIFEST_PATH = VOICE_DIR / "manifest.json"

# Default sentence the bake CLI speaks to produce a clone seed clip.
BAKE_SEED_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "She sells seashells by the seashore, and the "
    "voice you hear now is the one you will keep."
)
```

- [ ] **Step 5: Write `conftest.py` (skip GPU tests when no CUDA)**

```python
import pytest


def pytest_collection_modifyitems(config, items):
    try:
        import torch
        has_gpu = torch.cuda.is_available()
    except Exception:
        has_gpu = False
    if has_gpu:
        return
    skip = pytest.mark.skip(reason="no CUDA GPU available")
    for item in items:
        if "gpu" in item.keywords:
            item.add_marker(skip)
```

- [ ] **Step 6: Write a minimal `README.md`**

```markdown
# hosaka

Local near-real-time TTS. Kokoro presets + Chatterbox cloning, Parler
offline voice-baking. See `docs/superpowers/plans` for the build plan and
`docs/API.md` for the HTTP API.
```

- [ ] **Step 7: Verify pytest runs (no tests yet)**

Run: `cd ~/src/hosaka && python3.12 -m pytest`
Expected: exit 0, "no tests ran".

- [ ] **Step 8: Commit**

```bash
cd ~/src/hosaka
git add -A
git commit -m "chore: scaffold hosaka project skeleton"
```

---

### Task 2: Sentence-fragment chunker

The <1s lever: split input so fragment 1 is short and synthesized first.

**Files:**
- Create: `hosaka/chunking.py`
- Test: `tests/test_chunking.py`

**Interfaces:**
- Produces: `split_fragments(text: str, first_max_chars: int = 60) -> list[str]`
  — returns non-empty fragments; the FIRST fragment is kept short (split at the
  earliest sentence boundary, else at `first_max_chars` on a word boundary) so
  it synthesizes fast; remaining text is grouped into sentence-ish fragments.

- [ ] **Step 1: Write the failing test**

```python
from hosaka.chunking import split_fragments


def test_single_short_sentence_is_one_fragment():
    assert split_fragments("Hello there.") == ["Hello there."]


def test_first_fragment_breaks_at_first_sentence():
    out = split_fragments("Hi. This is the rest of a longer thought here.")
    assert out[0] == "Hi."
    assert "".join(out) == "Hi. This is the rest of a longer thought here."


def test_long_first_clause_breaks_on_word_boundary():
    text = "x " * 80  # 160 chars, no punctuation
    out = split_fragments(text.strip(), first_max_chars=60)
    assert len(out[0]) <= 60
    assert not out[0].endswith(" ")
    assert "".join(s if s.endswith(" ") else s + " " for s in out).strip() \
        == text.strip()


def test_empty_text_returns_empty_list():
    assert split_fragments("   ") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_chunking.py -v`
Expected: FAIL — `ModuleNotFoundError: hosaka.chunking`.

- [ ] **Step 3: Write minimal implementation**

```python
import re

_SENT_END = re.compile(r"(?<=[.!?])\s+")


def split_fragments(text: str, first_max_chars: int = 60) -> list[str]:
    text = text.strip()
    if not text:
        return []

    sentences = _SENT_END.split(text)
    sentences = [s.strip() for s in sentences if s.strip()]

    first = sentences[0]
    rest = sentences[1:]

    # If the first sentence is too long, hard-split it on a word boundary.
    if len(first) > first_max_chars:
        head, _, tail = _word_split(first, first_max_chars)
        out = [head]
        if tail:
            out.append(tail)
        out.extend(rest)
        return out

    return [first, *rest]


def _word_split(s: str, limit: int) -> tuple[str, str, str]:
    if len(s) <= limit:
        return s, "", ""
    cut = s.rfind(" ", 0, limit)
    if cut == -1:
        cut = limit
    return s[:cut].rstrip(), " ", s[cut:].lstrip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_chunking.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/chunking.py tests/test_chunking.py
git commit -m "feat: add sentence-fragment chunker for low-latency first audio"
```

---

### Task 3: Voice library + manifest

**Files:**
- Create: `hosaka/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces:
  - `@dataclass VoiceEntry: id: str; path: str; source: str; params: dict; created: str`
  - `class VoiceLibrary(root: Path)` with:
    - `.list() -> list[VoiceEntry]`
    - `.get(voice_id: str) -> VoiceEntry | None`
    - `.add(voice_id, wav_path: Path, source: str, params: dict | None = None, created: str = "") -> VoiceEntry` (copies the WAV into `root`, updates `manifest.json`)
    - `.path_for(voice_id) -> Path | None`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from hosaka.library import VoiceLibrary


def _make_wav(p: Path):
    p.write_bytes(b"RIFFfakewav")


def test_add_then_get_and_list(tmp_path):
    src = tmp_path / "src.wav"
    _make_wav(src)
    lib = VoiceLibrary(tmp_path / "voices")

    entry = lib.add("calm_brit", src, source="bake",
                    params={"exaggeration": 0.4}, created="2026-06-22")

    assert entry.id == "calm_brit"
    assert Path(entry.path).exists()
    assert lib.get("calm_brit").source == "bake"
    assert [e.id for e in lib.list()] == ["calm_brit"]


def test_persists_across_instances(tmp_path):
    src = tmp_path / "s.wav"
    _make_wav(src)
    root = tmp_path / "voices"
    VoiceLibrary(root).add("v1", src, source="recording")

    reopened = VoiceLibrary(root)
    assert reopened.get("v1") is not None


def test_get_missing_returns_none(tmp_path):
    assert VoiceLibrary(tmp_path / "voices").get("nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_library.py -v`
Expected: FAIL — `ModuleNotFoundError: hosaka.library`.

- [ ] **Step 3: Write minimal implementation**

```python
import json
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class VoiceEntry:
    id: str
    path: str
    source: str            # "recording" | "bake" | "kokoro"
    params: dict
    created: str


class VoiceLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / "manifest.json"

    def _read(self) -> dict:
        if not self.manifest.exists():
            return {}
        return json.loads(self.manifest.read_text())

    def _write(self, data: dict) -> None:
        tmp = self.manifest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.manifest)

    def add(self, voice_id, wav_path, source, params=None, created="") -> VoiceEntry:
        dest = self.root / f"{voice_id}.wav"
        shutil.copyfile(wav_path, dest)
        entry = VoiceEntry(voice_id, str(dest), source, params or {}, created)
        data = self._read()
        data[voice_id] = asdict(entry)
        self._write(data)
        return entry

    def get(self, voice_id) -> VoiceEntry | None:
        raw = self._read().get(voice_id)
        return VoiceEntry(**raw) if raw else None

    def list(self) -> list[VoiceEntry]:
        return [VoiceEntry(**v) for v in self._read().values()]

    def path_for(self, voice_id) -> Path | None:
        e = self.get(voice_id)
        return Path(e.path) if e else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_library.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/library.py tests/test_library.py
git commit -m "feat: add voice library with json manifest"
```

---

### Task 4: REPL command parser

**Files:**
- Create: `hosaka/cli/replcmd.py`
- Test: `tests/test_replcmd.py`

**Interfaces:**
- Produces:
  - `@dataclass ReplAction: kind: str; value: object = None` where `kind` is one
    of `"speak"`, `"voice"`, `"clone"`, `"set_param"`, `"voices"`, `"backend"`,
    `"help"`, `"quit"`, `"quit_stop"`, `"error"`. For `"set_param"`, `value` is
    a `(name, float)` tuple. For `"speak"`/`"voice"`/`"clone"`/`"backend"`,
    `value` is the string argument.
  - `parse_line(line: str) -> ReplAction`

- [ ] **Step 1: Write the failing test**

```python
from hosaka.cli.replcmd import parse_line


def test_plain_text_is_speak():
    a = parse_line("Hello world")
    assert (a.kind, a.value) == ("speak", "Hello world")


def test_voice_command():
    assert parse_line(":voice af_bella").value == "af_bella"
    assert parse_line(":voice af_bella").kind == "voice"


def test_set_param_parses_float():
    a = parse_line(":exag 0.7")
    assert a.kind == "set_param"
    assert a.value == ("exaggeration", 0.7)


def test_param_aliases():
    assert parse_line(":cfg 0.4").value == ("cfg_weight", 0.4)
    assert parse_line(":temp 0.8").value == ("temperature", 0.8)
    assert parse_line(":speed 1.2").value == ("speed", 1.2)


def test_bad_param_value_is_error():
    assert parse_line(":exag fast").kind == "error"


def test_quit_variants():
    assert parse_line(":quit").kind == "quit"
    assert parse_line(":quit --stop").kind == "quit_stop"


def test_unknown_command_is_error():
    assert parse_line(":frobnicate").kind == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_replcmd.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
from dataclasses import dataclass

_PARAMS = {
    "exag": "exaggeration",
    "cfg": "cfg_weight",
    "temp": "temperature",
    "speed": "speed",
}


@dataclass
class ReplAction:
    kind: str
    value: object = None


def parse_line(line: str) -> ReplAction:
    line = line.strip()
    if not line:
        return ReplAction("speak", "")
    if not line.startswith(":"):
        return ReplAction("speak", line)

    parts = line[1:].split()
    cmd, args = parts[0], parts[1:]

    if cmd in _PARAMS:
        if len(args) != 1:
            return ReplAction("error", f"usage: :{cmd} <number>")
        try:
            return ReplAction("set_param", (_PARAMS[cmd], float(args[0])))
        except ValueError:
            return ReplAction("error", f":{cmd} needs a number")
    if cmd == "voice":
        return ReplAction("voice", args[0]) if args else ReplAction("error", "usage: :voice <name>")
    if cmd == "clone":
        return ReplAction("clone", args[0]) if args else ReplAction("error", "usage: :clone <id|path>")
    if cmd == "backend":
        if args and args[0] in ("kokoro", "chatterbox"):
            return ReplAction("backend", args[0])
        return ReplAction("error", "usage: :backend kokoro|chatterbox")
    if cmd == "voices":
        return ReplAction("voices")
    if cmd == "help":
        return ReplAction("help")
    if cmd == "quit":
        return ReplAction("quit_stop") if args == ["--stop"] else ReplAction("quit")
    return ReplAction("error", f"unknown command: :{cmd}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_replcmd.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/cli/replcmd.py tests/test_replcmd.py
git commit -m "feat: add REPL colon-command parser"
```

---

### Task 5: Request schemas + param clamping

**Files:**
- Create: `hosaka/schemas.py`
- Test: `tests/test_schemas.py`
- Add dep: `pydantic` (already pulled by FastAPI in Task 6; for pure-logic tests install it in the dev environment now: `pip install pydantic`).

**Interfaces:**
- Produces:
  - `class SpeechParams(BaseModel)`: `exaggeration: float = 0.5`, `cfg_weight: float = 0.4`, `temperature: float = 0.8`, `speed: float = 1.0`
  - `class SpeechRequest(BaseModel)`: `input: str`, `backend: str = "kokoro"`, `voice: str = "af_heart"`, `params: SpeechParams = SpeechParams()`, `response_format: str = "pcm"`, `stream: bool = True`
  - `class VoiceInfo(BaseModel)`: `id: str`, `backend: str`, `source: str`
  - `clamp_params(params: SpeechParams) -> SpeechParams` — clamps each field to a safe range.

- [ ] **Step 1: Write the failing test**

```python
from hosaka.schemas import SpeechRequest, SpeechParams, clamp_params


def test_request_defaults():
    r = SpeechRequest(input="hi")
    assert r.backend == "kokoro"
    assert r.voice == "af_heart"
    assert r.params.exaggeration == 0.5
    assert r.stream is True


def test_clamp_bounds_values():
    p = SpeechParams(exaggeration=5.0, cfg_weight=-1.0,
                     temperature=9.0, speed=0.01)
    c = clamp_params(p)
    assert 0.0 <= c.exaggeration <= 2.0
    assert 0.0 <= c.cfg_weight <= 1.0
    assert 0.1 <= c.temperature <= 2.0
    assert 0.5 <= c.speed <= 2.0


def test_clamp_leaves_valid_values():
    p = SpeechParams(exaggeration=0.6, cfg_weight=0.4,
                     temperature=0.8, speed=1.1)
    assert clamp_params(p) == p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: hosaka.schemas`.

- [ ] **Step 3: Write minimal implementation**

```python
from pydantic import BaseModel


class SpeechParams(BaseModel):
    exaggeration: float = 0.5
    cfg_weight: float = 0.4
    temperature: float = 0.8
    speed: float = 1.0


class SpeechRequest(BaseModel):
    input: str
    backend: str = "kokoro"
    voice: str = "af_heart"
    params: SpeechParams = SpeechParams()
    response_format: str = "pcm"
    stream: bool = True


class VoiceInfo(BaseModel):
    id: str
    backend: str
    source: str


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def clamp_params(params: SpeechParams) -> SpeechParams:
    return SpeechParams(
        exaggeration=_clamp(params.exaggeration, 0.0, 2.0),
        cfg_weight=_clamp(params.cfg_weight, 0.0, 1.0),
        temperature=_clamp(params.temperature, 0.1, 2.0),
        speed=_clamp(params.speed, 0.5, 2.0),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_schemas.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/schemas.py tests/test_schemas.py
git commit -m "feat: add request schemas and param clamping"
```

---

### Task 6: Server venv + GPU verification

This is an environment + verification task. Its deliverable is a working
`.venv-server` and a `verify_gpu.py` that proves real CUDA compute on sm_120.

**Files:**
- Create: `scripts/verify_gpu.py`

**Interfaces:**
- Produces: a runnable `.venv-server` with torch 2.9.1+cu128, Chatterbox
  streaming fork, Kokoro, FastAPI; `scripts/verify_gpu.py` exits 0 only when a
  real CUDA matmul succeeds and capability is `(12, 0)`.

- [ ] **Step 1: Create the server venv and install torch FIRST (cu128 index)**

```bash
cd ~/src/hosaka
python3.12 -m venv .venv-server
.venv-server/bin/pip install --upgrade pip
.venv-server/bin/pip install \
  torch==2.9.1+cu128 torchaudio==2.9.1+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
.venv-server/bin/pip install torchcodec
```

- [ ] **Step 2: Install Chatterbox (streaming fork) and Kokoro `--no-deps`, then re-add deps**

```bash
cd ~/src/hosaka
.venv-server/bin/pip install \
  "git+https://github.com/davidbrowne17/chatterbox-streaming.git" --no-deps
.venv-server/bin/pip install \
  transformers accelerate scipy numpy peft soundfile
.venv-server/bin/pip install kokoro --no-deps
.venv-server/bin/pip install soundfile numpy fastapi uvicorn httpx pydantic
sudo apt install -y espeak-ng pulseaudio-utils
```

- [ ] **Step 3: Write `scripts/verify_gpu.py`**

```python
import sys
import torch


def main() -> int:
    print("torch", torch.__version__, "cuda", torch.version.cuda)
    if not torch.cuda.is_available():
        print("FAIL: CUDA not available")
        return 1
    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    print("device", name, "capability", cap)
    if cap != (12, 0):
        print(f"WARN: expected sm_120 (12, 0), got {cap}")
    x = torch.randn(4096, 4096, device="cuda")
    val = (x @ x).sum().item()   # must not raise "no kernel image"
    print("matmul ok, sum =", val)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the verification (THE critical gate)**

Run: `~/src/hosaka/.venv-server/bin/python scripts/verify_gpu.py`
Expected: prints `torch 2.9.1+cu128`, `capability (12, 0)`, `matmul ok`. Exit 0.
If it raises "no kernel image is available" → torch was downgraded; redo Steps 1-2 strictly in order.

- [ ] **Step 5: Commit**

```bash
git add scripts/verify_gpu.py
git commit -m "build: add server venv and sm_120 GPU verification script"
```

---

### Task 7: Engine base protocol + registry

**Files:**
- Create: `hosaka/server/engines/base.py`
- Test: `tests/test_engines_gpu.py` (registry part is plain-logic; mark engine load tests `gpu`)

**Interfaces:**
- Produces:
  - `class Engine(Protocol)`: `def stream(self, text: str, voice: str, params: dict) -> Iterator[np.ndarray]` (yields float32 mono 24 kHz chunks); `def warmup(self) -> None`
  - `@dataclass class EngineRegistry`: `kokoro: Engine`, `chatterbox: Engine`; `.get(backend: str) -> Engine`; `.warmup_all() -> None`
  - A `FakeEngine` test double living in the test file (yields a fixed array) — used to test the registry without a GPU.

- [ ] **Step 1: Write the failing test (registry, no GPU)**

```python
import numpy as np
import pytest
from hosaka.server.engines.base import EngineRegistry


class FakeEngine:
    def __init__(self, tag):
        self.tag = tag
        self.warmed = False

    def stream(self, text, voice, params):
        yield np.zeros(8, dtype=np.float32)

    def warmup(self):
        self.warmed = True


def test_registry_routes_by_backend():
    reg = EngineRegistry(kokoro=FakeEngine("k"), chatterbox=FakeEngine("c"))
    assert reg.get("kokoro").tag == "k"
    assert reg.get("chatterbox").tag == "c"


def test_registry_unknown_backend_raises():
    reg = EngineRegistry(kokoro=FakeEngine("k"), chatterbox=FakeEngine("c"))
    with pytest.raises(KeyError):
        reg.get("bogus")


def test_warmup_all_warms_both():
    k, c = FakeEngine("k"), FakeEngine("c")
    EngineRegistry(kokoro=k, chatterbox=c).warmup_all()
    assert k.warmed and c.warmed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_engines_gpu.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
from dataclasses import dataclass
from typing import Iterator, Protocol
import numpy as np


class Engine(Protocol):
    def stream(self, text: str, voice: str, params: dict) -> Iterator[np.ndarray]:
        ...

    def warmup(self) -> None:
        ...


@dataclass
class EngineRegistry:
    kokoro: Engine
    chatterbox: Engine

    def get(self, backend: str) -> Engine:
        if backend == "kokoro":
            return self.kokoro
        if backend == "chatterbox":
            return self.chatterbox
        raise KeyError(f"unknown backend: {backend}")

    def warmup_all(self) -> None:
        self.kokoro.warmup()
        self.chatterbox.warmup()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_engines_gpu.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/server/engines/base.py tests/test_engines_gpu.py
git commit -m "feat: add engine protocol and registry"
```

---

### Task 8: Kokoro engine

**Files:**
- Create: `hosaka/server/engines/kokoro_engine.py`
- Test: append a GPU test to `tests/test_engines_gpu.py`

**Interfaces:**
- Consumes: `Engine` protocol (Task 7), `SAMPLE_RATE` (Task 1).
- Produces: `class KokoroEngine` implementing `stream()` and `warmup()`.
  `stream(text, voice, params)` yields float32 mono numpy chunks at 24 kHz,
  using `params.get("speed", 1.0)`.

- [ ] **Step 1: Write the failing GPU test**

```python
@pytest.mark.gpu
def test_kokoro_streams_audio():
    from hosaka.server.engines.kokoro_engine import KokoroEngine
    eng = KokoroEngine()
    eng.warmup()
    chunks = list(eng.stream("Hello there.", "af_heart", {"speed": 1.0}))
    assert chunks, "no audio produced"
    assert chunks[0].dtype == np.float32
    assert chunks[0].ndim == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/src/hosaka/.venv-server/bin/python -m pytest tests/test_engines_gpu.py -k kokoro -v`
Expected: FAIL — `ModuleNotFoundError: hosaka.server.engines.kokoro_engine`.

- [ ] **Step 3: Write minimal implementation**

```python
import numpy as np
from kokoro import KPipeline
from hosaka.config import SAMPLE_RATE, DEFAULT_VOICE


class KokoroEngine:
    """Kokoro-82M. Presets + speed. 24 kHz mono output."""

    def __init__(self, lang_code: str = "a"):  # 'a' = American English
        self._pipe = KPipeline(lang_code=lang_code)

    def warmup(self) -> None:
        for _ in self.stream("warm up.", DEFAULT_VOICE, {"speed": 1.0}):
            pass

    def stream(self, text, voice, params):
        speed = float(params.get("speed", 1.0))
        for _gs, _ps, audio in self._pipe(text, voice=voice, speed=speed):
            arr = audio.detach().cpu().numpy().astype(np.float32)
            yield np.ascontiguousarray(arr.reshape(-1))
```

Note: Kokoro outputs 24 kHz, matching `SAMPLE_RATE`. If a future Kokoro release
changes its rate, resample here.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/src/hosaka/.venv-server/bin/python -m pytest tests/test_engines_gpu.py -k kokoro -v`
Expected: PASS (first run downloads the model).

- [ ] **Step 5: Commit**

```bash
git add hosaka/server/engines/kokoro_engine.py tests/test_engines_gpu.py
git commit -m "feat: add Kokoro preset engine"
```

---

### Task 9: Chatterbox engine

**Files:**
- Create: `hosaka/server/engines/chatterbox_engine.py`
- Test: append a GPU test to `tests/test_engines_gpu.py`

**Interfaces:**
- Consumes: `Engine` protocol, `SAMPLE_RATE`, `VoiceLibrary` (Task 3).
- Produces: `class ChatterboxEngine(library: VoiceLibrary)` implementing
  `stream()`/`warmup()`. `voice` is a library voice-id; the engine resolves it
  to a reference WAV path via `library.path_for(voice)`. Applies
  `exaggeration`/`cfg_weight`/`temperature` from params and a ~20 ms crossfade
  between chunks.

- [ ] **Step 1: Write the failing GPU test**

```python
@pytest.mark.gpu
def test_chatterbox_clones_from_seed(tmp_path):
    # Build a seed clip from Kokoro, register it, then clone it.
    import soundfile as sf
    from hosaka.config import SAMPLE_RATE
    from hosaka.library import VoiceLibrary
    from hosaka.server.engines.kokoro_engine import KokoroEngine
    from hosaka.server.engines.chatterbox_engine import ChatterboxEngine

    seed = np.concatenate(list(
        KokoroEngine().stream(
            "This is a seed clip for cloning, spoken clearly.",
            "af_heart", {"speed": 1.0})))
    seed_path = tmp_path / "seed.wav"
    sf.write(seed_path, seed, SAMPLE_RATE)

    lib = VoiceLibrary(tmp_path / "voices")
    lib.add("seed1", seed_path, source="kokoro")

    eng = ChatterboxEngine(lib)
    chunks = list(eng.stream("Now cloned.", "seed1",
                             {"exaggeration": 0.5, "cfg_weight": 0.4,
                              "temperature": 0.8}))
    assert chunks and chunks[0].dtype == np.float32
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/src/hosaka/.venv-server/bin/python -m pytest tests/test_engines_gpu.py -k chatterbox -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
import numpy as np
import torch
from chatterbox.tts import ChatterboxTTS
from hosaka.config import SAMPLE_RATE, DEFAULT_VOICE
from hosaka.library import VoiceLibrary

_XFADE = int(0.020 * SAMPLE_RATE)   # 20 ms crossfade


class ChatterboxEngine:
    """Original Chatterbox via the streaming fork. Cloning + tuning."""

    def __init__(self, library: VoiceLibrary):
        self.library = library
        self.model = ChatterboxTTS.from_pretrained(device="cuda")
        self._seed_warm = None

    def warmup(self) -> None:
        # Warm up with a tiny utterance using the model's built-in voice
        # (no reference) so kernels/caches are allocated.
        for _ in self._raw_stream("warm up.", None,
                                  {"exaggeration": 0.5, "cfg_weight": 0.4,
                                   "temperature": 0.8}):
            pass

    def stream(self, text, voice, params):
        ref = self.library.path_for(voice)
        ref_path = str(ref) if ref else None
        prev_tail = None
        for chunk in self._raw_stream(text, ref_path, params):
            if prev_tail is not None and len(chunk) > _XFADE:
                fade = np.linspace(0.0, 1.0, _XFADE, dtype=np.float32)
                chunk[:_XFADE] = chunk[:_XFADE] * fade + prev_tail * (1.0 - fade)
            prev_tail = chunk[-_XFADE:].copy() if len(chunk) >= _XFADE else None
            yield chunk

    def _raw_stream(self, text, ref_path, params):
        kw = dict(
            exaggeration=float(params.get("exaggeration", 0.5)),
            cfg_weight=float(params.get("cfg_weight", 0.4)),
            temperature=float(params.get("temperature", 0.8)),
            chunk_size=50,
        )
        if ref_path:
            kw["audio_prompt_path"] = ref_path
        with torch.inference_mode():
            for audio_chunk, _metrics in self.model.generate_stream(text, **kw):
                arr = audio_chunk.detach().cpu().numpy().astype(np.float32)
                yield np.ascontiguousarray(arr.reshape(-1))
```

Note: confirm `generate_stream` signature and that the model sample rate
(`self.model.sr`) is 24 kHz against the streaming fork README at build time; if
the fork yields a different rate, resample to `SAMPLE_RATE` here. `_metrics`
carries `latency_to_first_chunk` used by the benchmark (Task 15).

- [ ] **Step 4: Run test to verify it passes**

Run: `~/src/hosaka/.venv-server/bin/python -m pytest tests/test_engines_gpu.py -k chatterbox -v`
Expected: PASS (downloads Chatterbox on first run).

- [ ] **Step 5: Commit**

```bash
git add hosaka/server/engines/chatterbox_engine.py tests/test_engines_gpu.py
git commit -m "feat: add Chatterbox cloning engine with crossfade"
```

---

### Task 10: FastAPI app — lifespan, /health, /v1/voices

**Files:**
- Create: `hosaka/server/app.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `EngineRegistry`, `VoiceLibrary`, `KOKORO_PRESETS` (define a small
  constant list in `app.py`), schemas.
- Produces:
  - `KOKORO_PRESETS: list[str]` (the shipped preset voice names)
  - `def stop_llm() -> None` (best-effort `ollama stop gpt-oss:20b`)
  - `def create_app(registry: EngineRegistry, library: VoiceLibrary, do_warmup: bool = True) -> FastAPI` with routes `/health`, `/v1/voices`, `/v1/audio/speech` (the last filled in Task 11).
  - On lifespan startup: call `stop_llm()` then `registry.warmup_all()` (when `do_warmup`).

- [ ] **Step 1: Write the failing test (uses FakeEngine, no GPU)**

```python
import numpy as np
from fastapi.testclient import TestClient
from hosaka.server.engines.base import EngineRegistry
from hosaka.library import VoiceLibrary
from hosaka.server.app import create_app


class FakeEngine:
    def stream(self, text, voice, params):
        yield np.zeros(1200, dtype=np.float32)

    def warmup(self):
        pass


def _client(tmp_path):
    reg = EngineRegistry(kokoro=FakeEngine(), chatterbox=FakeEngine())
    lib = VoiceLibrary(tmp_path / "voices")
    return TestClient(create_app(reg, lib, do_warmup=False)), lib


def test_health_ok(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_voices_lists_presets_and_library(tmp_path):
    client, lib = _client(tmp_path)
    seed = tmp_path / "s.wav"
    seed.write_bytes(b"RIFFfake")
    lib.add("myclone", seed, source="recording")
    ids = {v["id"] for v in client.get("/v1/voices").json()}
    assert "af_heart" in ids        # a preset
    assert "myclone" in ids         # a library clip
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: hosaka.server.app`.

- [ ] **Step 3: Write minimal implementation**

```python
import subprocess
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hosaka.config import LLM_MODEL
from hosaka.schemas import VoiceInfo
from hosaka.server.engines.base import EngineRegistry
from hosaka.library import VoiceLibrary

KOKORO_PRESETS = [
    "af_heart", "af_bella", "af_nicole", "af_sarah", "am_adam",
    "am_michael", "bf_emma", "bm_george",
]


def stop_llm() -> None:
    try:
        subprocess.run(["ollama", "stop", LLM_MODEL],
                       capture_output=True, timeout=30, check=False)
    except Exception:
        pass   # best-effort; logged by caller if needed


def create_app(registry: EngineRegistry, library: VoiceLibrary,
               do_warmup: bool = True) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if do_warmup:
            stop_llm()
            registry.warmup_all()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.registry = registry
    app.state.library = library

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/voices")
    def voices():
        out = [VoiceInfo(id=p, backend="kokoro", source="preset").model_dump()
               for p in KOKORO_PRESETS]
        out += [VoiceInfo(id=e.id, backend="chatterbox", source=e.source).model_dump()
                for e in library.list()]
        return out

    # /v1/audio/speech added in Task 11.
    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_server.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/server/app.py tests/test_server.py
git commit -m "feat: add FastAPI app with health, voices, and lifespan warmup"
```

---

### Task 11: Streaming /v1/audio/speech endpoint

**Files:**
- Modify: `hosaka/server/app.py` (add the route + a module-level `Semaphore`)
- Test: append to `tests/test_server.py`

**Interfaces:**
- Consumes: `split_fragments` (Task 2), `clamp_params`/`SpeechRequest` (Task 5),
  `EngineRegistry`.
- Produces: `POST /v1/audio/speech` returning a `StreamingResponse` of raw
  float32 LE PCM bytes (media type `application/octet-stream`). Splits `input`
  into fragments, synthesizes each via the chosen backend, streams chunks as
  produced. `503` when the single GPU slot is busy.

- [ ] **Step 1: Write the failing test**

```python
def test_speech_streams_pcm_bytes(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/v1/audio/speech",
                    json={"input": "Hello. World.", "backend": "kokoro",
                          "voice": "af_heart"})
    assert r.status_code == 200
    assert len(r.content) > 0
    assert len(r.content) % 4 == 0      # float32 == 4 bytes/sample


def test_speech_unknown_backend_is_400(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/v1/audio/speech",
                    json={"input": "hi", "backend": "bogus"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_server.py -k speech -v`
Expected: FAIL — 404 (route missing).

- [ ] **Step 3: Add the route to `create_app`**

Add these imports at the top of `app.py`:

```python
import asyncio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from hosaka.chunking import split_fragments
from hosaka.schemas import SpeechRequest, clamp_params
```

Insert this inside `create_app`, before `return app`:

```python
    gpu_lock = asyncio.Semaphore(1)

    @app.post("/v1/audio/speech")
    async def speech(req: SpeechRequest):
        try:
            engine = registry.get(req.backend)
        except KeyError:
            raise HTTPException(status_code=400,
                                detail=f"unknown backend: {req.backend}")
        if gpu_lock.locked():
            raise HTTPException(status_code=503, detail="busy")

        params = clamp_params(req.params).model_dump()
        fragments = split_fragments(req.input)

        async def gen():
            async with gpu_lock:
                loop = asyncio.get_running_loop()
                for frag in fragments:
                    queue: asyncio.Queue = asyncio.Queue()

                    def produce():
                        try:
                            for chunk in engine.stream(frag, req.voice, params):
                                loop.call_soon_threadsafe(
                                    queue.put_nowait, chunk.tobytes())
                        finally:
                            loop.call_soon_threadsafe(queue.put_nowait, None)

                    loop.run_in_executor(None, produce)
                    while True:
                        item = await queue.get()
                        if item is None:
                            break
                        yield item

        return StreamingResponse(gen(), media_type="application/octet-stream",
                                 headers={"X-Accel-Buffering": "no",
                                          "Cache-Control": "no-cache"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_server.py -k speech -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/server/app.py tests/test_server.py
git commit -m "feat: add streaming speech endpoint with fragment chunking"
```

---

### Task 12: pacat audio player

**Files:**
- Create: `hosaka/audio.py`
- Test: `tests/test_audio.py`

**Interfaces:**
- Produces: `class PacatPlayer` (context manager). `__enter__` spawns `pacat`
  (or an injected command), `.write(chunk: np.ndarray | bytes)` feeds float32 LE
  PCM to stdin, `.close()`/`__exit__` flushes and terminates. Accepts a
  `cmd: list[str] | None` override for testing (default builds the real `pacat`
  argv from `SAMPLE_RATE`).

- [ ] **Step 1: Write the failing test (uses `cat` as a fake player)**

```python
import numpy as np
from pathlib import Path
from hosaka.audio import PacatPlayer


def test_player_writes_bytes_to_subprocess(tmp_path):
    out = tmp_path / "out.raw"
    # Fake player: shell that copies stdin to a file.
    cmd = ["sh", "-c", f"cat > {out}"]
    data = np.ones(100, dtype=np.float32)
    with PacatPlayer(cmd=cmd) as p:
        p.write(data)
    assert out.exists()
    assert out.stat().st_size == data.nbytes


def test_default_cmd_targets_pacat():
    p = PacatPlayer()
    assert p.cmd[0] == "pacat"
    assert "--rate=24000" in p.cmd
    assert "--format=float32le" in p.cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_audio.py -v`
Expected: FAIL — `ModuleNotFoundError: hosaka.audio`.

- [ ] **Step 3: Write minimal implementation**

```python
import subprocess
import numpy as np
from hosaka.config import SAMPLE_RATE


class PacatPlayer:
    def __init__(self, cmd: list[str] | None = None, latency_msec: int = 50):
        self.cmd = cmd or [
            "pacat", "--raw",
            f"--rate={SAMPLE_RATE}", "--channels=1",
            "--format=float32le", f"--latency-msec={latency_msec}",
        ]
        self._proc: subprocess.Popen | None = None

    def __enter__(self):
        self._proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, bufsize=0)
        return self

    def write(self, chunk) -> None:
        if isinstance(chunk, np.ndarray):
            chunk = chunk.astype("<f4").tobytes()
        self._proc.stdin.write(chunk)

    def close(self) -> None:
        if self._proc and self._proc.stdin:
            self._proc.stdin.close()
            self._proc.wait(timeout=10)
            self._proc = None

    def __exit__(self, *exc):
        self.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_audio.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add hosaka/audio.py tests/test_audio.py
git commit -m "feat: add pacat streaming audio player"
```

---

### Task 13: REPL client

**Files:**
- Create: `hosaka/cli/repl.py`
- Test: manual smoke (interactive + audio); no unit test (thin glue over tested units).

**Interfaces:**
- Consumes: `parse_line`/`ReplAction` (Task 4), `PacatPlayer` (Task 12),
  `config` (Task 1), the HTTP API (Tasks 10-11) via `httpx`.
- Produces: `def main()` console entry. Holds mutable session state
  (`backend`, `voice`, `params`); auto-spawns the server if `/health` fails;
  spawns one `PacatPlayer`; loops reading stdin lines through `parse_line` and
  dispatches.

- [ ] **Step 1: Write the client**

```python
import subprocess
import sys
import time
import httpx
from hosaka.config import (SERVER_URL, DEFAULT_BACKEND, DEFAULT_VOICE,
                           VOICE_DIR)
from hosaka.audio import PacatPlayer
from hosaka.cli.replcmd import parse_line
from hosaka.library import VoiceLibrary


def _server_up() -> bool:
    try:
        return httpx.get(f"{SERVER_URL}/health", timeout=1.0).status_code == 200
    except Exception:
        return False


def _spawn_server() -> None:
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hosaka.server.main:app",
         "--host", "127.0.0.1", "--port", SERVER_URL.rsplit(":", 1)[1]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):          # wait up to ~60s for model load + warmup
        if _server_up():
            return
        time.sleep(0.5)
    raise RuntimeError("server did not become healthy")


def _speak(player, backend, voice, params, text):
    body = {"input": text, "backend": backend, "voice": voice,
            "params": params, "stream": True}
    with httpx.stream("POST", f"{SERVER_URL}/v1/audio/speech",
                      json=body, timeout=None) as r:
        if r.status_code != 200:
            print(f"[server {r.status_code}] {r.read().decode(errors='ignore')}")
            return
        for raw in r.iter_bytes():
            if raw:
                player.write(raw)


def main():
    if not _server_up():
        print("starting hosaka server (loading models)...")
        _spawn_server()

    lib = VoiceLibrary(VOICE_DIR)
    backend = DEFAULT_BACKEND
    voice = DEFAULT_VOICE
    params = {"exaggeration": 0.5, "cfg_weight": 0.4,
              "temperature": 0.8, "speed": 1.0}

    print("hosaka ready. Type to speak; :help for commands. (^D to quit)")
    with PacatPlayer() as player:
        for line in sys.stdin:
            a = parse_line(line.rstrip("\n"))
            if a.kind == "speak":
                if a.value:
                    _speak(player, backend, voice, params, a.value)
            elif a.kind == "voice":
                voice, backend = a.value, "kokoro"
            elif a.kind == "clone":
                # path -> add to library; else treat as existing id
                from pathlib import Path
                p = Path(a.value)
                if p.exists():
                    vid = p.stem
                    lib.add(vid, p, source="recording")
                    voice = vid
                else:
                    voice = a.value
                backend = "chatterbox"
            elif a.kind == "backend":
                backend = a.value
            elif a.kind == "set_param":
                name, val = a.value
                params[name] = val
            elif a.kind == "voices":
                for v in httpx.get(f"{SERVER_URL}/v1/voices").json():
                    print(f"  {v['id']:20s} {v['backend']:11s} {v['source']}")
            elif a.kind == "help":
                print(":voice <name> | :clone <id|path> | :backend k|c | "
                      ":exag/:cfg/:temp/:speed <f> | :voices | :quit[ --stop]")
            elif a.kind == "error":
                print(f"  {a.value}")
            elif a.kind in ("quit", "quit_stop"):
                if a.kind == "quit_stop":
                    try:
                        httpx.post(f"{SERVER_URL}/shutdown", timeout=1.0)
                    except Exception:
                        pass
                break


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create the uvicorn entry module `hosaka/server/main.py`**

```python
from hosaka.config import VOICE_DIR
from hosaka.library import VoiceLibrary
from hosaka.server.app import create_app
from hosaka.server.engines.base import EngineRegistry
from hosaka.server.engines.kokoro_engine import KokoroEngine
from hosaka.server.engines.chatterbox_engine import ChatterboxEngine

_library = VoiceLibrary(VOICE_DIR)
_registry = EngineRegistry(
    kokoro=KokoroEngine(),
    chatterbox=ChatterboxEngine(_library),
)
app = create_app(_registry, _library, do_warmup=True)
```

- [ ] **Step 3: Smoke test end-to-end (requires GPU + audio)**

Run:
```bash
cd ~/src/hosaka
.venv-server/bin/python -m hosaka.cli.repl
```
Type `Hello, this is hosaka speaking.` and press enter.
Expected: audible speech within ~1s (after the one-time model load on first
launch). Try `:voice bf_emma`, then a sentence; hear a different voice.

- [ ] **Step 4: Commit**

```bash
git add hosaka/cli/repl.py hosaka/server/main.py
git commit -m "feat: add REPL client with server auto-spawn and audio streaming"
```

---

### Task 14: Optional /shutdown route (clean `:quit --stop`)

**Files:**
- Modify: `hosaka/server/app.py` (add `/shutdown`)
- Test: append to `tests/test_server.py`

**Interfaces:**
- Produces: `POST /shutdown` that signals the process to exit (sets an event the
  uvicorn server checks, or calls `os.kill(os.getpid(), signal.SIGTERM)`).

- [ ] **Step 1: Write the failing test**

```python
def test_shutdown_route_exists(tmp_path):
    client, _ = _client(tmp_path)
    # We don't actually kill the test process: monkeypatch the killer.
    import hosaka.server.app as appmod
    called = {}
    appmod._do_shutdown = lambda: called.setdefault("hit", True)
    r = client.post("/shutdown")
    assert r.status_code == 200
    assert called.get("hit")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_server.py -k shutdown -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

Add to `app.py`:

```python
import os
import signal


def _do_shutdown():
    os.kill(os.getpid(), signal.SIGTERM)
```

And inside `create_app`, before `return app`:

```python
    @app.post("/shutdown")
    def shutdown():
        _do_shutdown()
        return {"status": "stopping"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_server.py -k shutdown -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hosaka/server/app.py tests/test_server.py
git commit -m "feat: add server shutdown route for clean quit --stop"
```

---

### Task 15: Latency benchmark — the <1s gate (Risk 1)

**Files:**
- Create: `scripts/benchmark_latency.py`

**Interfaces:**
- Consumes: both engines.
- Produces: a script that, for each backend, measures wall time from
  `stream()` call to the FIRST yielded chunk (warm), prints results, and exits
  non-zero if Chatterbox first-chunk consistently exceeds 1000 ms — surfacing
  the fallback decision (Turbo / XTTS-v2).

- [ ] **Step 1: Write `scripts/benchmark_latency.py`**

```python
import sys
import time
import statistics
import numpy as np
import soundfile as sf
from pathlib import Path
from hosaka.config import SAMPLE_RATE
from hosaka.library import VoiceLibrary
from hosaka.server.engines.kokoro_engine import KokoroEngine
from hosaka.server.engines.chatterbox_engine import ChatterboxEngine

SHORT = "Hello, this is a short first sentence."
RUNS = 5


def first_chunk_ms(stream_iter) -> float:
    t0 = time.perf_counter()
    next(stream_iter)
    return (time.perf_counter() - t0) * 1000.0


def bench(label, make_iter) -> list[float]:
    make_iter()                              # warm
    times = [first_chunk_ms(make_iter()) for _ in range(RUNS)]
    print(f"{label}: first-chunk ms "
          f"min={min(times):.0f} median={statistics.median(times):.0f} "
          f"max={max(times):.0f}")
    return times


def main(tmp="/tmp/hosaka_bench") -> int:
    Path(tmp).mkdir(exist_ok=True)
    kok = KokoroEngine()
    bench("kokoro", lambda: kok.stream(SHORT, "af_heart", {"speed": 1.0}))

    seed = np.concatenate(list(
        kok.stream("Seed clip for cloning, spoken clearly and calmly.",
                   "af_heart", {"speed": 1.0})))
    seed_path = Path(tmp) / "seed.wav"
    sf.write(seed_path, seed, SAMPLE_RATE)
    lib = VoiceLibrary(Path(tmp) / "voices")
    lib.add("bench_seed", seed_path, source="kokoro")

    cb = ChatterboxEngine(lib)
    cb_times = bench(
        "chatterbox",
        lambda: cb.stream(SHORT, "bench_seed",
                          {"exaggeration": 0.5, "cfg_weight": 0.4,
                           "temperature": 0.8}))

    if statistics.median(cb_times) > 1000.0:
        print("GATE FAIL: Chatterbox median first-chunk > 1s. "
              "Consider fallback: Turbo (loses knobs) or XTTS-v2.")
        return 1
    print("GATE PASS: Chatterbox first-chunk within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the benchmark on the box**

Run: `~/src/hosaka/.venv-server/bin/python scripts/benchmark_latency.py`
Expected: prints per-backend first-chunk timings. Records the real number that
decides Risk 1. If GATE FAIL, raise the fallback decision with the user.

- [ ] **Step 3: Commit**

```bash
git add scripts/benchmark_latency.py
git commit -m "test: add first-chunk latency benchmark (the <1s gate)"
```

---

### Task 16: Bake venv + Parler bake CLI

**Files:**
- Create: `hosaka/cli/bake.py`
- (Environment) create `.venv-bake`

**Interfaces:**
- Consumes: `config.BAKE_SEED_TEXT`, `config.VOICE_DIR`, `VoiceLibrary`.
- Produces: `def main()` — `hosaka-bake "<description>" --out <id> [--text <seed sentence>]`
  → runs Parler offline → writes `<VOICE_DIR>/<id>.wav` at 24 kHz mono → registers
  it in the manifest with `source="bake"` and the description in `params`.
- Runs ONLY in `.venv-bake`. The live server never imports this module.

- [ ] **Step 1: Create the isolated bake venv**

```bash
cd ~/src/hosaka
python3.12 -m venv .venv-bake
.venv-bake/bin/pip install --upgrade pip
.venv-bake/bin/pip install \
  torch==2.9.1+cu128 torchaudio==2.9.1+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
.venv-bake/bin/pip install parler-tts --no-deps
.venv-bake/bin/pip install transformers==4.46.1 descript-audio-codec soundfile numpy
```

- [ ] **Step 2: Verify the bake venv GPU works**

Run: `~/src/hosaka/.venv-bake/bin/python scripts/verify_gpu.py`
Expected: capability (12, 0), matmul ok. If it fails, the bake path is broken
but the server is unaffected (Risk 3) — report to user, do not block.

- [ ] **Step 3: Write `hosaka/cli/bake.py`**

```python
import argparse
import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly
from parler_tts import ParlerTTSForConditionalGeneration
from transformers import AutoTokenizer
from hosaka.config import BAKE_SEED_TEXT, VOICE_DIR, SAMPLE_RATE
from hosaka.library import VoiceLibrary

_MODEL = "parler-tts/parler-tts-mini-v1.1"


def bake(description: str, voice_id: str, text: str = BAKE_SEED_TEXT) -> str:
    model = ParlerTTSForConditionalGeneration.from_pretrained(_MODEL).to("cuda")
    tok = AutoTokenizer.from_pretrained(_MODEL)
    desc_ids = tok(description, return_tensors="pt").input_ids.to("cuda")
    prompt_ids = tok(text, return_tensors="pt").input_ids.to("cuda")
    with torch.inference_mode():
        audio = model.generate(input_ids=desc_ids,
                               prompt_input_ids=prompt_ids)
    wav = audio.cpu().numpy().squeeze().astype(np.float32)

    src_sr = model.config.sampling_rate          # Parler emits 44.1 kHz
    if src_sr != SAMPLE_RATE:
        wav = resample_poly(wav, SAMPLE_RATE, src_sr).astype(np.float32)

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = VOICE_DIR / f"{voice_id}.wav"
    sf.write(out_path, wav, SAMPLE_RATE)
    VoiceLibrary(VOICE_DIR).add(voice_id, out_path, source="bake",
                               params={"description": description})
    return str(out_path)


def main():
    ap = argparse.ArgumentParser(prog="hosaka-bake")
    ap.add_argument("description", help="natural-language voice description")
    ap.add_argument("--out", required=True, help="voice id to save as")
    ap.add_argument("--text", default=BAKE_SEED_TEXT,
                    help="sentence to speak for the seed clip")
    args = ap.parse_args()
    path = bake(args.description, args.out, args.text)
    print(f"baked voice '{args.out}' -> {path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Bake a test voice end-to-end**

Run:
```bash
~/src/hosaka/.venv-bake/bin/python -m hosaka.cli.bake \
  "A calm, deep British male voice, very clear audio, moderate pace." \
  --out calm_brit
```
Expected: prints `baked voice 'calm_brit' -> ~/.local/share/hosaka/voices/calm_brit.wav`.
Then in the REPL: `:clone calm_brit`, speak a line, hear the baked voice cloned.

- [ ] **Step 5: Commit**

```bash
git add hosaka/cli/bake.py
git commit -m "feat: add Parler offline voice-bake CLI"
```

---

### Task 17: Sample seeds, API docs, README, entry points

**Files:**
- Create: `hosaka/sample_voices/README.md` (note on shipped seeds), 1-2 sample WAVs (generated via Kokoro)
- Create: `docs/API.md`
- Modify: `README.md`, `pyproject.toml` (console entry points)

**Interfaces:**
- Produces: `hosaka` and `hosaka-bake` console scripts; `docs/API.md` documenting
  `/health`, `/v1/voices`, `/v1/audio/speech` for a future frontend.

- [ ] **Step 1: Generate sample seed clips with Kokoro**

```bash
~/src/hosaka/.venv-server/bin/python - <<'PY'
import numpy as np, soundfile as sf
from pathlib import Path
from hosaka.config import SAMPLE_RATE
from hosaka.server.engines.kokoro_engine import KokoroEngine
eng = KokoroEngine()
for vid, voice in [("sample_us", "af_heart"), ("sample_gb", "bf_emma")]:
    wav = np.concatenate(list(eng.stream(
        "This is a sample seed voice shipped with hosaka.",
        voice, {"speed": 1.0})))
    sf.write(Path("hosaka/sample_voices")/f"{vid}.wav", wav, SAMPLE_RATE)
print("sample seeds written")
PY
```

- [ ] **Step 2: Write `docs/API.md`**

```markdown
# hosaka HTTP API

Base URL: `http://127.0.0.1:8123`. Local only. PCM is float32 LE, 24 kHz, mono.

## GET /health
Returns `{"status": "ok"}` once both models are loaded and warmed.

## GET /v1/voices
Returns a list of `{id, backend, source}`. `backend` is `kokoro` (presets) or
`chatterbox` (cloneable library clips). `source` is `preset|recording|bake|kokoro`.

## POST /v1/audio/speech
Request body:
```json
{
  "input": "text to speak",
  "backend": "kokoro | chatterbox",
  "voice": "af_heart | <library voice id>",
  "params": {"exaggeration": 0.5, "cfg_weight": 0.4,
             "temperature": 0.8, "speed": 1.0},
  "response_format": "pcm",
  "stream": true
}
```
Response: `200` with `Content-Type: application/octet-stream`, a chunked stream
of raw float32 LE PCM (24 kHz mono). Concatenate chunks and play, or feed a
PCM player directly. Returns `503` if the single GPU slot is busy, `400` for an
unknown backend.

Note for a future browser frontend: PCM + no CORS in v1. A browser client will
need a `wav`/`mp3` `response_format` and CORS headers added server-side.
```

- [ ] **Step 3: Update `README.md` with install + usage**

Document: the two-venv install (link the exact commands from Tasks 6 and 16),
the `verify_gpu.py` gate, `pulseaudio-utils` (never the daemon), running the
REPL, the colon-commands, and baking a voice. Note the accepted constraints
(Chatterbox watermark, no pitch, ~500ms-1s clone latency).

- [ ] **Step 4: Add console entry points to `pyproject.toml`**

```toml
[project.scripts]
hosaka = "hosaka.cli.repl:main"
hosaka-bake = "hosaka.cli.bake:main"
```

- [ ] **Step 5: Run the full non-GPU test suite**

Run: `cd ~/src/hosaka && python3.12 -m pytest -m "not gpu" -v`
Expected: all pure-logic tests PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: add API reference, README, sample seeds, entry points"
```

---

## Self-Review

**Spec coverage:**
- Bake-once architecture → Tasks 9 (clone), 16 (bake), data flow honored.
- Kokoro presets + fast path → Task 8, presets in Task 10.
- Chatterbox cloning + tuning (original model, knobs) → Task 9.
- Parler offline isolated venv → Task 16 (separate `.venv-bake`).
- HTTP server (lifespan, warmup, ollama stop, /health, /v1/voices, streaming
  speech, Semaphore(1), 503) → Tasks 10, 11, 14.
- Sentence-fragment chunking (the <1s lever) → Task 2, applied Task 11.
- REPL with colon-commands, auto-spawn, default kokoro/af_heart → Tasks 4, 13.
- pacat audio (client only) → Task 12, install in Task 6.
- sm_120 install recipe + real-matmul verify → Tasks 6, 16.
- Voice library untracked in ~/.local/share + sample seeds in repo → Tasks 3,
  13, 17; `.gitignore` in Task 1.
- API doc for future frontend → Task 17.
- Latency benchmark gate (Risk 1) → Task 15.
- 24 kHz float32 PCM everywhere → `config.SAMPLE_RATE`, used throughout.

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N" — each
step carries real code or exact commands.

**Type consistency:** `split_fragments`, `VoiceLibrary.add/get/path_for/list`,
`VoiceEntry`, `parse_line`/`ReplAction`, `SpeechRequest`/`SpeechParams`/
`clamp_params`/`VoiceInfo`, `Engine`/`EngineRegistry.get/warmup_all`,
`KokoroEngine.stream`, `ChatterboxEngine(library).stream`, `create_app(registry,
library, do_warmup)`, `KOKORO_PRESETS`, `stop_llm`, `PacatPlayer(cmd)` — names
and signatures consistent across tasks that consume them.

**Known build-time confirmations (flagged in-task, not placeholders):**
- Chatterbox streaming fork `generate_stream` signature + output sample rate
  (Task 9) — confirm against the fork README; resample if not 24 kHz.
- Kokoro `KPipeline` yield shape (Task 8) — confirm against installed version.
- Parler `generate` call + `config.sampling_rate` (Task 16) — resample handled.
