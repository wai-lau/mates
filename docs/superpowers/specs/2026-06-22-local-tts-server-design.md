# hosaka — Local TTS Server — Design

**Date:** 2026-06-22
**Status:** Approved design, pre-implementation
**Project:** `hosaka` — new standalone git repo at `~/src/hosaka`
**Author:** wai + Claude

## Goal

A local text-to-speech tool with a tunable, choosable voice that speaks
near-real-time: from pressing enter to first audio in under ~1 second.
Runs entirely on-device (RTX 5070 Ti, 16 GB), English only.

Four voice-control modes, all wanted:

1. **Preset voices** — pick from a library of built-in named voices.
2. **Voice cloning** — drop in a short reference clip; output mimics it.
3. **Param tuning** — adjust emotion intensity, pacing, speed.
4. **Text style-prompt** — describe a voice in words; synthesize a matching
   voice. *(Design-time only — see "bake-once" below.)*

## Hardware / environment (verified on box)

| Item | Value |
|------|-------|
| GPU | RTX 5070 Ti, 16303 MiB, Blackwell, compute capability **sm_120** |
| Driver | 591.86 (exposes CUDA 12.8+ into WSL2 — sufficient, no host action) |
| OS | Windows 11 + WSL2 (Ubuntu), Python 3.12.3 |
| Audio | WSLg `/mnt/wslg/PulseServer` socket healthy, `PULSE_SERVER` set |
| Present | `ffplay`. **Missing:** `pacat`/`paplay` (need `pulseaudio-utils`) |

Standalone tool: runs when gpt-oss (the LLM, ~15 GB) is **not** loaded, so the
full 16 GB is free. On server start, hosaka frees VRAM by stopping the LLM
(`ollama stop gpt-oss:20b`, best-effort).

## Core architectural decision: bake-once voice designer

Text style-prompt models (Parler-TTS) generate autoregressively — multi-second
per utterance, *every* time, resident or not. Keeping them warm removes load
latency, not generation latency. So they cannot serve the live <1s path.

Resolution: move the slow step to **design time, once**.

```
Text description ("calm deep British male")
   │  Parler-TTS, slow, ONE time, offline
   ▼
seed audio WAV  ──► saved as a named "voice"
   │
   ▼  Chatterbox clone, FAST, every utterance
<1s speech, live
```

This unifies all four controls onto a fast live path:

- **Presets** = Kokoro's native voices (a dedicated fast engine).
- **Cloning** = Chatterbox clones any reference clip (recorded, curated, baked).
- **Param tuning** = Chatterbox `exaggeration`/`cfg_weight`/`temperature`, Kokoro `speed`.
- **Style-prompt** = Parler bakes a seed clip offline → it becomes a clone-able voice.

The per-utterance path is **always** a fast engine. The slow engine only ever
runs offline.

## Engine stack

Selected after a 16-agent research workflow with adversarial verification of
the two load-bearing claims per engine (real <1s latency, real sm_120 support).

| Role | Engine | Notes |
|------|--------|-------|
| Presets + fast path | **Kokoro-82M** (hexgrad) | ~28 English voices (20 US + 8 GB), RTF ~0.04–0.06, ~150–400 ms first chunk. Apache-2.0. ~2–3 GB VRAM. **Default voice: `af_heart`.** |
| Cloning + tuning | **Chatterbox** original model, via `davidbrowne17/chatterbox-streaming` fork | best open zero-shot clone from 5–10 s clip; keeps `exaggeration`/`cfg_weight`/`temperature`. MIT. ~2–3 GB VRAM. sm_120 confirmed on the exact RTX 5070 Ti (resemble-ai/chatterbox#344). |
| Style-prompt (offline bake) | **Parler-TTS Mini v1.1** | text-described voice → 44.1 kHz seed WAV (resampled to 24 kHz on save). Apache-2.0. **Isolated venv, offline CLI only.** |

Both Kokoro and Chatterbox stay pinned in VRAM simultaneously (~5–6 GB of 16).

**Why these, not alternates:**
- XTTS-v2 (idiap) — viable fallback #1, but needs DeepSpeed-on-Blackwell rebuild
  + a `transformers` pin that fights modern stacks.
- F5-TTS — best clone fidelity but 1–2 s to first packet by design (diffusion) →
  fails live <1s. Offline only.
- Orpheus — cleanest license, but vLLM/SGLang path is the most Blackwell-fragile
  option found. Avoid.

**Use original Chatterbox, NOT Turbo.** Turbo silently ignores `exaggeration` and
`cfg_weight` — killing two of the four required controls. Turbo is reserved as a
speed escape hatch only (see Risk 1).

## Project layout

```
~/src/hosaka/
  hosaka/
    server/          # FastAPI app: model loading, streaming, endpoints
      app.py
      engines/
        kokoro_engine.py
        chatterbox_engine.py
      chunking.py    # sentence-fragment splitter (the <1s lever)
      library.py     # voice-library manifest read/write
    cli/
      repl.py        # persistent REPL client + colon-commands
      bake.py        # offline Parler bake CLI (runs in venv-bake)
    audio.py         # pacat player wrapper
  docs/
    API.md           # HTTP API reference for a future frontend
  tests/
  .venv-server/      # gitignored — Kokoro + Chatterbox
  .venv-bake/        # gitignored — Parler (isolated)
  README.md
  pyproject.toml
  .gitignore
```

User voice data lives **outside the repo** (privacy — personal recordings never
hit git):

```
~/.local/share/hosaka/voices/
  manifest.json      # voice-id → {path, source, params, created}
  <id>.wav           # 24 kHz mono seed clips
```

Repo ships only a couple sample seeds under `hosaka/sample_voices/` for
out-of-box cloning; the server copies them into the user dir on first run.

## Components

Three deployable units, clear boundaries.

### 1. TTS server (`.venv-server`: Kokoro + Chatterbox)

FastAPI + Uvicorn, single resident process.

- **Startup (`lifespan`):**
  1. Best-effort `ollama stop gpt-oss:20b` to free VRAM.
  2. Load both models into VRAM, run a tiny warmup synth on each (dodge
     cold-start). Pinned for the session, never unloaded on idle.
- **Endpoints (OpenAI-shaped where possible; full reference in `docs/API.md`):**
  - `POST /v1/audio/speech` — synthesize + stream. Body:
    ```json
    {
      "input": "text to speak",
      "backend": "kokoro" | "chatterbox",
      "voice": "af_heart" | "<reference-clip-id>",
      "params": { "exaggeration": 0.5, "cfg_weight": 0.4,
                  "temperature": 0.8, "speed": 1.0 },
      "response_format": "pcm",
      "stream": true
    }
    ```
  - `GET /v1/voices` — list available presets + library clips (for REPL
    `:voices` and a future frontend).
  - `GET /health` — readiness (models loaded + warmed). The CLI polls this
    after auto-spawning the server.
- **The real <1s lever:** server splits `input` into sentence fragments and
  force-emits fragment 1 immediately. This matters more than any model's
  internal "streaming" flag (Chatterbox streaming is chunk-level → no first-byte
  win on short text; Kokoro raw speed carries it).
- **Streaming mechanism:** blocking synth runs in `run_in_executor`, pushes PCM
  chunks into an `asyncio.Queue`; async `StreamingResponse` drains and yields.
- **Output:** raw PCM 24 kHz mono (no encode/decode latency on localhost).
- **Concurrency:** `asyncio.Semaphore(1)` (single user), `503` if busy.
- **~20 ms crossfade** between Chatterbox chunks to hide boundary clicks.
- Anti-buffering headers (`X-Accel-Buffering: no`, `Cache-Control: no-cache`).

### 2. CLI client — persistent REPL (`.venv-server`)

- Launch once: checks `/health`; if the server is down, **auto-spawns it**
  (subprocess), waits until healthy, then connects.
- Spawns `pacat --raw` **once per session**, pipes streamed PCM straight in:
  ```
  pacat --raw --rate=24000 --channels=1 \
        --format=float32le --latency-msec=50
  ```
- Read loop: a line **not** starting with `:` is spoken. Lines starting with `:`
  are control commands:

  | Command | Effect |
  |---------|--------|
  | `:voice <name>` | switch to a Kokoro preset (e.g. `af_bella`) |
  | `:clone <id\|path>` | switch to a library clip, or add+use an arbitrary WAV |
  | `:exag <f>` `:cfg <f>` `:temp <f>` `:speed <f>` | set Chatterbox/Kokoro params |
  | `:voices` | list presets + library clips |
  | `:backend <kokoro\|chatterbox>` | force backend (auto-inferred from voice otherwise) |
  | `:help` | show command list |
  | `:quit` | exit (server keeps running, or `:quit --stop` to also stop it) |

  Default on launch: `backend=kokoro`, `voice=af_heart`.

### 3. Bake CLI (`.venv-bake`: Parler, isolated)

- `hosaka-bake "<style prompt>" --out <name>` → runs Parler offline → writes a
  24 kHz mono seed WAV to `~/.local/share/hosaka/voices/<id>.wav` and registers
  it in `manifest.json`.
- **Lives in a separate venv.** The live server never imports Parler.

## Data flow

```
preset name  ─► server(Kokoro)     ─► PCM ─┐
clip/voice-id─► server(Chatterbox) ─► PCM ─┴► HTTP chunked ─► REPL ─► pacat ─► out
text desc    ─► bake CLI(Parler)   ─► seed.wav ─► voice library ─► (clone-able)
```

## Install recipe (sm_120)

**Do NOT** `apt install` any Linux GPU driver, the `cuda`/`cuda-drivers`
meta-packages, or `pulseaudio` (the daemon) inside WSL — each breaks the
environment (`/dev/dxg` clobber; WSLg audio "too many symbolic links" bug).

**Mandatory rule:** install torch from the cu128 index FIRST, then each TTS
package with `--no-deps`, then re-add non-torch deps. Omitting `--index-url`
silently installs CPU/old-CUDA torch → "no kernel image" → silent CPU fallback.

**Server venv:**
```
cd ~/src/hosaka
python3.12 -m venv .venv-server
.venv-server/bin/pip install \
  torch==2.9.1+cu128 torchaudio==2.9.1+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
.venv-server/bin/pip install torchcodec
.venv-server/bin/pip install \
  git+https://github.com/davidbrowne17/chatterbox-streaming.git --no-deps
.venv-server/bin/pip install \
  transformers accelerate scipy numpy peft soundfile
.venv-server/bin/pip install kokoro --no-deps
.venv-server/bin/pip install soundfile numpy fastapi uvicorn httpx
sudo apt install -y espeak-ng        # Kokoro G2P
```

**Bake venv (isolated):**
```
cd ~/src/hosaka
python3.12 -m venv .venv-bake
.venv-bake/bin/pip install \
  torch==2.9.1+cu128 torchaudio==2.9.1+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
.venv-bake/bin/pip install parler-tts --no-deps
.venv-bake/bin/pip install \
  transformers==4.46.1 descript-audio-codec soundfile
```

**Audio client tools:**
```
sudo apt install -y pulseaudio-utils   # pacat/paplay — client only, NOT the daemon
```

**Mandatory verification** (`is_available()` is NOT proof — a broken stack
returns True then crashes on real compute):
```
python - <<'PY'
import torch
print(torch.__version__, torch.version.cuda)
print(torch.cuda.get_device_name(0))
print(torch.cuda.get_device_capability(0))   # expect (12, 0)
x = torch.randn(4096, 4096, device='cuda')
print((x @ x).sum().item())                   # must NOT raise
PY
```

## Accepted constraints

1. **Chatterbox clone latency ~500 ms–1 s, not 300 ms.** <1s is achievable but
   is the single biggest risk. The 472 ms figure is a native-Linux 4090
   self-report; the 5070 Ti is below 4090 class and WSL2 adds overhead. Must be
   benchmarked on the box. Fallback ladder defined in Risk 1.
2. **No native pitch control on any engine.** Tuning levers: Chatterbox
   `exaggeration` (emotion; higher = faster), `cfg_weight` (pacing; lower =
   slower), `temperature`; Kokoro `speed`. User confirmed these are sufficient —
   no pitch slider required.
3. **Chatterbox watermarks every output** (Resemble Perth, imperceptible,
   non-removable). Accepted for local use. Kokoro/Parler outputs unwatermarked.
4. **Parler is the least-maintained component** (frozen ~18 mo, hard-pins
   `transformers==4.46.1`, no confirmed RTX-50 success). Mitigated by isolated
   venv + offline-only use → zero blast radius to the live server.

## Risk register

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Chatterbox clone first-chunk latency brushes/exceeds 1s on this card+WSL2. | High | Streaming fork (mandatory) + startup warmup + short first fragment. **Benchmark `latency_to_first_chunk` on the box early.** If it misses consistently: fallback ladder Turbo (loses knobs) → XTTS-v2. Decided after benchmark. |
| 2 | `pip install` silently downgrades torch (Chatterbox pins 2.6.0; Parler drags CPU torch) → "no kernel image" → silent CPU fallback. | High | torch-cu128-FIRST then `--no-deps`; run the real-matmul verification; never trust `is_available()`. Pin everything. |
| 3 | Parler `transformers==4.46.1` ↔ torch 2.9 coexistence unverified on Blackwell. | Med-High | Isolated venv; offline CLI only. If it won't run, the live server is unaffected; mint seeds from recordings/Kokoro clips instead. |
| 4 | `apt install pulseaudio` (daemon) breaks WSLg audio → silence. | Med | Install only `pulseaudio-utils`. If broken: `apt remove pulseaudio`, `wsl --shutdown`, re-link `native` symlink. Prefer `pacat`. |
| 5 | Chatterbox internal streaming gives no first-byte win on short utterances. | Med | Server-side sentence-fragment chunking is the actual mechanism. |
| 6 | Both models pinned to 16 GB. | Low | ~5–6 GB total; `Semaphore(1)`. Non-issue at current scale. |
| 7 | torchaudio 2.9 moved `load`/`save` to torchcodec. | Low | `torchcodec` installed; engines mostly use `soundfile`. |
| 8 | Mandatory Perth watermark on Chatterbox output. | Low (constraint) | Inherent; accepted for local use. |
| 9 | Auto `ollama stop` kills an in-use LLM. | Low | Best-effort + logged; single-user box, TTS use implies LLM not active. |

## Testing strategy

- **Unit:** sentence-fragment splitter; voice-library manifest read/write; param
  validation/clamping; request schema parsing; REPL command parser.
- **Integration (require GPU):** server startup stops LLM + loads both models +
  warmup; `/v1/audio/speech` returns valid PCM for each backend; `/v1/voices`
  lists correctly; bake CLI produces a clone-able WAV; clone of a baked seed runs
  through Chatterbox; CLI auto-spawn + `/health` poll works.
- **Latency benchmark (the gate):** measure to-first-audio-chunk per backend,
  warm, short first sentence, on the actual box. This decides Risk 1.
- **Smoke:** end-to-end REPL line → audible speech through `pacat`.

## Deliverables

- `hosaka` REPL client, `hosaka-server`, `hosaka-bake` CLIs.
- `docs/API.md` — HTTP API reference (endpoints, request/response schemas,
  streaming format) so a future frontend can consume the server.
- `README.md` — install recipe + usage.

## Out of scope (v1)

- Multilingual (English only).
- Concurrent/multi-user streams (`Semaphore(1)`, single user).
- A built frontend — but the HTTP API is documented in `docs/API.md` for one
  later. Browser consumption will additionally need CORS + a `wav`/`mp3`
  `response_format`; deferred until the frontend exists.
- True pitch shifting (constraint 2; revisit with a post-process stage if needed).
- Co-residence with the LLM in VRAM.

## Open items — resolved

- Project: **new standalone repo `~/src/hosaka`.**
- CLI: **persistent REPL with inline colon-commands.** Default `kokoro`/`af_heart`.
- Server lifecycle: **CLI auto-spawns the server**, polls `/health`.
- LLM VRAM: **auto `ollama stop gpt-oss:20b` on server start** (best-effort).
- Voice data: **`~/.local/share/hosaka/voices/`, untracked**; repo ships a couple
  sample seeds only.
- Seed library sourced from: **own recordings + Parler-baked + curated Kokoro clips.**
- Parler: **build in v1** (isolated venv).
- Chatterbox latency fallback: **build original-model path; decide fallback after
  benchmarking on the box.**
- Tuning levers: **exaggeration + cfg_weight + speed sufficient; no pitch.**
- API: **documented in `docs/API.md` for a future frontend.**
