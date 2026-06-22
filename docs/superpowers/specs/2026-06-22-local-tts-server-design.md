# Local TTS Server — Design

**Date:** 2026-06-22
**Status:** Approved design, pre-implementation
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
full 16 GB is free. Not designed to co-reside with the LLM.

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
- **Cloning** = Chatterbox clones any reference clip (recorded, curated, or baked).
- **Param tuning** = Chatterbox `exaggeration`/`cfg_weight`/`temperature`, Kokoro `speed`.
- **Style-prompt** = Parler bakes a seed clip offline → it becomes a clone-able voice.

The per-utterance path is **always** a fast engine. The slow engine only ever
runs offline.

## Engine stack

Selected after a 16-agent research workflow with adversarial verification of
the two load-bearing claims per engine (real <1s latency, real sm_120 support).

| Role | Engine | Notes |
|------|--------|-------|
| Presets + fast path | **Kokoro-82M** (hexgrad) | ~28 English voices (20 US + 8 GB), RTF ~0.04–0.06, ~150–400 ms first chunk. Apache-2.0. ~2–3 GB VRAM. |
| Cloning + tuning | **Chatterbox** original model, via `davidbrowne17/chatterbox-streaming` fork | best open zero-shot clone from 5–10 s clip; keeps `exaggeration`/`cfg_weight`/`temperature`. MIT. ~2–3 GB VRAM. sm_120 confirmed on the exact RTX 5070 Ti (GitHub resemble-ai/chatterbox#344). |
| Style-prompt (offline bake) | **Parler-TTS Mini v1.1** | text-described voice → 44.1 kHz seed WAV. Apache-2.0. **Isolated venv, offline CLI only.** |

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

## Components

Three deployable units, clear boundaries:

### 1. TTS server (`venv-server`: Kokoro + Chatterbox)

FastAPI + Uvicorn, single resident process.

- **Startup (`lifespan`):** load both models into VRAM, run a tiny warmup synth
  on each to dodge cold-start. Never unload on idle — pinned for the session.
- **Endpoint:** `POST /v1/audio/speech` (OpenAI-shaped):
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

### 2. CLI client (thin)

- Reads a line (the "press enter" UX), POSTs with `stream=True`.
- Spawns `pacat --raw` **once per session**, pipes streamed PCM straight into it.
  ```
  pacat --raw --rate=24000 --channels=1 \
        --format=float32le --latency-msec=50
  ```
- Flags for `--backend`, `--voice`, `--exaggeration`, etc.

### 3. Bake CLI (`venv-bake`: Parler, isolated)

- `bake "<style prompt>" --out seeds/<name>.wav` → runs Parler offline → writes
  a reference clip the server's Chatterbox backend can later clone.
- **Lives in a separate venv.** The live server never imports Parler.
- Output WAV is registered into the voice library (a directory of seed clips +
  a small JSON manifest mapping voice-id → clip path).

## Data flow

```
preset name  ─► server(Kokoro)     ─► PCM ─┐
clip/voice-id─► server(Chatterbox) ─► PCM ─┴► HTTP chunked ─► CLI ─► pacat ─► out
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
python3.12 -m venv ~/tts/venv-server
~/tts/venv-server/bin/pip install \
  torch==2.9.1+cu128 torchaudio==2.9.1+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
~/tts/venv-server/bin/pip install torchcodec
~/tts/venv-server/bin/pip install \
  git+https://github.com/davidbrowne17/chatterbox-streaming.git --no-deps
~/tts/venv-server/bin/pip install \
  transformers accelerate scipy numpy peft soundfile
~/tts/venv-server/bin/pip install kokoro --no-deps
~/tts/venv-server/bin/pip install soundfile numpy
sudo apt install -y espeak-ng        # Kokoro G2P
```

**Bake venv (isolated):**
```
python3.12 -m venv ~/tts/venv-bake
~/tts/venv-bake/bin/pip install \
  torch==2.9.1+cu128 torchaudio==2.9.1+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
~/tts/venv-bake/bin/pip install parler-tts --no-deps
~/tts/venv-bake/bin/pip install \
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

## Testing strategy

- **Unit:** sentence-fragment splitter; voice-library manifest read/write;
  param validation/clamping; request schema parsing.
- **Integration (require GPU):** server startup loads both models + warmup
  succeeds; `/v1/audio/speech` returns valid PCM for each backend; bake CLI
  produces a clone-able WAV; clone of a baked seed runs through Chatterbox.
- **Latency benchmark (the gate):** measure to-first-audio-chunk per backend,
  warm, short first sentence, on the actual box. This decides Risk 1.
- **Smoke:** end-to-end CLI → audible speech through `pacat`.

## Out of scope (v1)

- Multilingual (English only).
- Concurrent/multi-user streams (`Semaphore(1)`, single user).
- Browser/MP3 output (raw PCM to local CLI only; add encode later if needed).
- True pitch shifting (constraint 2; revisit with a post-process stage if ever
  needed).
- Co-residence with the LLM in VRAM.

## Open items resolved

- Parler: **build in v1** (isolated venv).
- Chatterbox latency fallback: **build original-model path; decide fallback
  after benchmarking on the box.**
- Tuning levers: **exaggeration + cfg_weight + speed are sufficient; no pitch.**
