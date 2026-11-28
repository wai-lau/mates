# Local LLM Learning

Running local LLMs on one Windows + WSL2 workstation: what runs, how to use it,
and what was tried. Updated 2026-11-18.

- Daily use of the local coding model: [`docs/qwen.md`](docs/qwen.md)
- Every model tried, with measurements: [`docs/models.md`](docs/models.md)
- `mates` (`bin/mates`): Claude Code as the lead in the left tmux pane, sandboxed
  Qwen (OpenCode) panes on the right, each in its own worktree; the lead sends,
  checks, diffs and merges their work. Spec:
  [`docs/superpowers/specs/2026-11-10-claude-qwen-mates-design.md`](docs/superpowers/specs/2026-11-10-claude-qwen-mates-design.md)
  (the two-pane revision)
- How mates works, its trust boundaries and a task's lifecycle: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Retired tools and finished plans: [`ARCHAEOLOGY.md`](ARCHAEOLOGY.md)
- The TTS server (hosaka): [`docs/superpowers/specs/2026-06-22-local-tts-server-design.md`](docs/superpowers/specs/2026-06-22-local-tts-server-design.md)

## Hardware

| Component | Spec |
|---|---|
| GPU 0 | NVIDIA RTX 5070 Ti, 16 GB (Blackwell, sm_120), about 14.7 GB free with the Windows desktop up |
| GPU 1 | NVIDIA GTX 1660, 6 GB (Turing, sm_75), about 5 GB free |
| WSL2 RAM | 39 GB |
| OS | Windows + WSL2, Ubuntu 24.04 |

Both GPUs together give about 19.7 GB for a model. With 39 GB of RAM, a model
that spills to CPU no longer risks an OOM kill; it only gets slower (see
`docs/models.md`). Mixture-of-experts models run well with their experts in RAM.

## Current setup

| Piece | What |
|---|---|
| Runtime | Ollama 0.40.16, system service on `127.0.0.1:11434` (settings in `/etc/systemd/system/ollama.service.d/override.conf`) |
| Coding model | `qwen3.8-27b-turbo-heretic:mtp-iq4xs`, 64K context (why: `docs/models.md`) |
| Coding agent | OpenCode 1.19.5 (`~/.local/opt/opencode-1.19.5/`), run through `qwen` inside a bubblewrap sandbox |
| Old default | `gpt-oss:20b` (still installed: emet's extractor and hosaka use it, with `nomic-embed-text`) |
| Model definition | `ollama/qwen3.8-27b-turbo-heretic-mtp-iq4xs.Modelfile` |

`qwen` runs its own private Ollama server on a free port (found again by
reading its process environment, never a fixed number). The system service is
left alone. Qwen takes the 5070 Ti from Charlie (hosaka's quality voice) and
only Charlie: hosaka TTS keeps serving nicole on the GTX 1660 beside it, and
`qwen stop` brings Charlie back when homo was asked.

## Daily commands

```
qwen                 # OpenCode on the local model, sandboxed, in the current git repo
qwen exec -- <cmd>   # run a command in the sandbox (no server, no model), return its exit code
qwen server          # start or find the private server, print its address
qwen stop            # stop the private server (Charlie back if homo was asked)
qwen sandbox-test    # print what the sandbox can and cannot reach
qwen --help          # the full rules
```

Checks:

```
ollama ps                                         # system server: what is loaded
OLLAMA_HOST=$(qwen server) ollama ps              # private server: model, CPU/GPU split, context
nvidia-smi                                        # memory per GPU
free -h                                           # WSL RAM
journalctl -u ollama -n 50                        # system server log
tail ~/.local/state/qwen/serve.log                # private server log
```

## Ollama service config

`/etc/systemd/system/ollama.service.d/override.conf`:

```
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_CONTEXT_LENGTH=8192"
```

The private `qwen` server overrides the ones that matter for a coding agent:
one slot (`NUM_PARALLEL=1`), an 8-bit KV cache (`KV_CACHE_TYPE=q8_0`), spread
across both GPUs (`SCHED_SPREAD=1`), 60-minute keep-alive, and the context
from the model's Modelfile.

## What didn't work

- Default Q4 `qwen3.8:27b` from the Ollama library (17 GB): spills 5.3 GB
  to CPU even at 32K and generates at 2 tok/s. Replaced by smaller quants.
- unsloth GGUF chat templates with Claude Code: their Jinja template raises
  "System message must be at the beginning" on Claude Code's multi-part system
  prompt, so every request is HTTP 500. Fix: import the weights with Ollama's
  own `RENDERER qwen3.8` / `PARSER qwen3.5` (see `ollama/*.Modelfile`).
- Long Hugging Face repo names: Ollama rejects them ("invalid model name").
  Download the GGUF with `curl` and `ollama create` from the file.
- Two workers at once on one 27B model: 0.4-0.5 tok/s each. One at a time.
- Older attempts (gpt-oss era): the `gpt_oss.chat` reference
  implementation needs unpublished `triton_kernels`; Homebrew's llama.cpp is
  CPU-only (the official llama.cpp CUDA build works, in
  `~/.local/opt/llama.cpp-b12257/` until 2026-11-10, but Ollama's automatic MoE placement made it
  unnecessary).

## Files outside this repo

| Path | What |
|---|---|
| `~/bin/qwen` | symlink to `bin/qwen` here |
| `~/.config/qwen/copy-in` | files copied into the sandbox at each launch |
| `~/.config/qwen/digests/` | short digests of CLAUDE.md files, by hash |
| `~/bin/mates` | symlink to `bin/mates` here |
| `~/bin/claud` | symlink to `bin/claud` here (a Claude follower in a pane to the right) |
| `~/.local/state/qwen/` | server log, OpenCode sessions (one folder per repo), GPU locks |
| `~/.local/opt/opencode-1.19.5/` | OpenCode binary |
| `~/.claude/mates/` | mates state per repo and the task log (local only) |
| `~/.local/state/mates/` | mates runtimes and check snapshots (only the owner, Wai, can write there) |
| `/srv/mates/` | the old pilot's shared area; no longer used, can be deleted |

## Cleanup if abandoning

```
qwen stop
ollama rm qwen3.8-27b-turbo-heretic:mtp-iq4xs
rm -rf ~/.local/opt/opencode-* ~/.config/qwen ~/.local/state/qwen ~/.local/state/mates ~/.claude/mates
rm ~/bin/mates ~/bin/claud
rm ~/bin/qwen
```
