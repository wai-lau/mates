# Local LLM Learning

Notes from setting up local LLM inference on this machine. What works, what failed, and the daily-use commands.

## Hardware

| Component | Spec |
|-----------|------|
| GPU | NVIDIA RTX 5070 Ti, 16 GB VRAM (Blackwell, SM 12.0) |
| Host RAM | 16 GB |
| WSL2 RAM | 12 GB (via `.wslconfig`) |
| OS | Windows + WSL2 (Ubuntu) |

Hard rule: model must fit 100% in VRAM. Any CPU offload → kernel OOM-kills due to tight 8 GB default WSL RAM (now 12 GB but still tight with 12 GB models).

## Current Setup

- **Runtime:** Ollama 0.30.0 (systemd service)
- **Model:** `gpt-oss:20b` (mxfp4, ~13 GB on disk, ~15 GB VRAM at runtime)
- **CLI agent:** aider 0.86.2 (Python venv at `~/aider-venv`)

## How to Use

### Interactive chat

```
ollama run gpt-oss:20b
```

In-chat commands:
- `/set parameter reasoning low` — fast mode (less reasoning before reply)
- `/set system "..."` — set system prompt
- `/show info` — model details
- `/clear` — reset conversation
- `/bye` — exit CLI (model stays loaded in VRAM)

### HTTP API (OpenAI-compatible)

Endpoint: `http://localhost:11434/v1`

```
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"gpt-oss:20b",
    "messages":[{"role":"user","content":"hi"}],
    "stream":false
  }'
```

Native Ollama API:
```
curl http://localhost:11434/api/chat -d '{
  "model":"gpt-oss:20b",
  "messages":[{"role":"user","content":"hi"}],
  "options":{"reasoning":"low"}
}'
```

### Coding agent (aider)

In any git repo:
```
cd /path/to/repo
aider
```

Common in-aider commands: `/add <file>`, `/drop`, `/diff`, `/run <cmd>`, `/lint`, `/test`, `/web <url>`, `/help`.

Config files:
- `~/.aider.conf.yml` — model + defaults
- `~/.aider.env` — `OLLAMA_API_BASE`, `OLLAMA_CONTEXT_LENGTH`

### Status checks

```
ollama ps                  # what's loaded, VRAM use, processor split
ollama list                # installed models
nvidia-smi                 # VRAM + GPU util
free -h                    # WSL RAM
journalctl -u ollama -n 50 # service logs
```

`ollama ps` PROCESSOR column **must show `100% GPU`**. Anything else = CPU offload = will OOM.

### Lifecycle

- Model loads on first request, stays resident until manual stop or service restart (`OLLAMA_KEEP_ALIVE=-1`).
- `ollama stop gpt-oss:20b` — free VRAM.
- `sudo systemctl restart ollama` — full reset.
- `wsl --shutdown` (PowerShell) — release WSL VM RAM back to Windows.

## Ollama Service Config

systemd drop-in at `/etc/systemd/system/ollama.service.d/override.conf`:

```
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
```

| Var | Effect |
|-----|--------|
| `OLLAMA_KEEP_ALIVE=-1` | Model stays in VRAM forever (no 5-min auto-unload) |
| `OLLAMA_FLASH_ATTENTION=1` | Flash Attention kernel → lower KV mem, faster on Blackwell |
| `OLLAMA_MAX_LOADED_MODELS=1` | One model resident at a time (prevents VRAM oversubscription) |
| `OLLAMA_NUM_PARALLEL=1` | Single concurrent slot (solo user; saves KV VRAM) |

## Model Customization

Modelfile params applied to `gpt-oss:20b`:

```
PARAMETER use_mmap false   # weights load straight to VRAM, skip page cache thrash
PARAMETER num_gpu 999      # all layers on GPU
```

Build pattern:
```
ollama show gpt-oss:20b --modelfile > /tmp/mf
# edit /tmp/mf — change FROM to model name (not blob path), add PARAMETER lines
ollama create gpt-oss:20b -f /tmp/mf
```

## WSL2 RAM Config

`C:\Users\wailu\.wslconfig`:
```
[wsl2]
memory=12GB
swap=8GB
```

Apply: `wsl --shutdown` in PowerShell → reopen WSL.

Trade-off: WSL VM commits up to 12 GB host RAM permanently while running. `wsl --shutdown` releases it.

## What We Tried That Didn't Work

### `gpt_oss.chat` reference impl (OpenAI Python package)
- Path: `pip install gpt-oss[triton]` → `python -m gpt_oss.chat`
- Failed: needs `triton_kernels` package not on PyPI — must build from triton repo source.
- Torch backend per README needs 4× H100s (for 120b; 20b unclear but slow).
- Abandoned.

### Brew-installed llama.cpp
- `llama-cli` from `brew install llama.cpp` → CPU-only build, `-ngl 99` ignored.
- 12 GB model + 8 GB RAM → OOM kill on load.
- Would need rebuild from source with `cmake -DGGML_CUDA=ON` + CUDA toolkit (Blackwell needs ≥12.8).
- Skipped in favor of Ollama (precompiled CUDA, simpler).

### mxfp4 GGUF + llama.cpp
- Downloaded `ggml-org/gpt-oss-20b-GGUF` → unused because llama.cpp was CPU-only.
- Could import into Ollama as Modelfile, but Ollama's own pull is more foolproof (correct harmony chat template baked in).
- Deleted to reclaim disk.

## Performance Notes

- Generation speed: ~143 tok/s on `gpt-oss:20b` (RTX 5070 Ti) — excellent for a 20B model.
- Perceived slowness usually = gpt-oss reasoning channel (medium effort by default). Lower with `reasoning=low` for fast replies.
- VRAM at idle (model loaded): ~15 GB / 16.3 GB. Free ~950 MB.
- Don't increase context (`num_ctx`) without budget — KV cache grows and risks CUDA OOM.

## Model Choice Reasoning

`gpt-oss:20b` benchmarks well in this VRAM class:
- 77.7% LiveCodeBench, 89.3% math, 97% pass rate on a 38-task real coding benchmark.
- Beats DeepSeek-R1 distills, Llama 3.3 (won't fit anyway), Phi-4, Mistral Small 3.x for coding/reasoning.

Secondary fast-lane candidate: `qwen3:14b` (q4_K_M, ~9 GB). Toggleable thinking via `/think` `/no_think`. Lower reasoning latency than gpt-oss for chat.

Won't fit cleanly: Qwen3 32B q4 (~19 GB), Gemma 3 27B q4 (~17 GB w/o KV room), Llama 3.3 70B.

## Files Created

- `~/.aider.conf.yml`, `~/.aider.env` — aider config
- `~/aider-venv/` — aider Python venv
- `~/bin/aider` → symlink into venv
- `~/gpt-oss-venv/` — OpenAI gpt_oss venv (kept for `hf` CLI; can delete)
- `/etc/systemd/system/ollama.service.d/override.conf` — Ollama service tuning
- `/mnt/c/Users/wailu/.wslconfig` — WSL2 12 GB RAM

## Cleanup If Abandoning

```
ollama rm gpt-oss:20b
sudo systemctl disable --now ollama
sudo rm -rf /usr/local/bin/ollama /usr/share/ollama /etc/systemd/system/ollama.service*
rm -rf ~/aider-venv ~/gpt-oss-venv ~/bin/aider
rm ~/.aider.conf.yml ~/.aider.env
# Optional: revert ~/.wslconfig memory line, then wsl --shutdown
```
