# Archaeology

What was tried here and then retired, and where the remains are. The live
setup is in [`README.md`](README.md). Local-only leftovers (finished plans,
old chat logs) sit in the gitignored `.archaeology/` folder at the repo root.

## Retired 2026-11-11

| What | Why it went | Where it is now |
|---|---|---|
| mates Phase 1 pilot (`mates/pilot.py`, `tasks.py`, `fwd.py`, `ctx.py`, `sandbox.sh`, worker env and settings, pilot prompts and their tests) | Wai skipped the pilot for the two-pane mates launcher, which imports none of it | removed in `381e325` on branch `mates-phase1` (git history before it); local copy `.archaeology/mates-phase1/` |

## Retired 2026-11-10

| What | Why it went | Where it is now |
|---|---|---|
| aider as the agent behind `qwen aider` | OpenCode became the default (it runs in the same sandbox) and aider fell out of use | code: `bin/qwen` at commit `9a453b2` (the `qwen aider` branch; it left `bin/qwen` when branch `mates-phase1` merged); its old chat history and tag cache: `.archaeology/aider/` |
| hosaka TTS build plan (`docs/superpowers/plans/2026-06-22-hosaka-tts.md`) | finished; hosaka runs from its own repo `~/src/hosaka` | `.archaeology/plans/`, plus its task briefs and reports in `.archaeology/plans/hosaka-sdd/`; tracked copy in git history before this commit. The design spec stays in `docs/superpowers/specs/` |
| `fib_stickman.py` | an empty file from the first commit | git history only |

### aider: what it took to run it

Kept here in case aider comes back:

- aider 0.86.2 lived in `~/aider-venv/`, and `qwen aider` ran it inside the same
  bwrap sandbox, talking to Ollama as `ollama_chat/<model>` with
  `--weak-model` set to the same model (otherwise `~/.aider.conf.yml`'s weak
  model loaded a second model onto the GPUs).
- It needed a model-settings file with `num_ctx` set, or aider picked a small
  context of its own.
- For a model it doesn't know, aider appends its SEARCH/REPLACE rules to the
  user's last message, so the model thinks you pasted them. The fix was
  `reminder: sys` and `examples_as_sys_msg: true` in that settings file.
- Its clipboard (pyperclip) sees WSL and copies with `clip.exe`. The sandbox
  had a `clip.exe` stand-in for that, next to the `xclip` one OpenCode uses.

## Deleted 2026-11-10 (70 GB freed)

| What | Size | To get it back |
|---|---|---|
| `~/.cache/mates-models/turbo-mtp-iq4xs.gguf` | 16 GB | re-download with the `curl` line at the top of `ollama/qwen3.8-27b-turbo-heretic-mtp-iq4xs.Modelfile` (only needed to rebuild that model) |
| `~/.local/opt/llama.cpp-b12257/` | 1.8 GB | the official llama.cpp CUDA release b12257 |
| Ollama models `qwen3.8-mates` (plain Qwen3.8-27B, unsloth UD-IQ4_XS, the first mates worker) and `qwen3.6:35b-a3b` | 35 GB | their Modelfiles are in `.archaeology/ollama/`; `qwen3.8-mates` needs the unsloth GGUF re-downloaded, `qwen3.6:35b-a3b` is `ollama pull` |
| Ollama model `qwen3.8:27b` (library Q4, too slow: `docs/models.md`) | 16 GB | `ollama pull qwen3.8:27b` |
| Ollama models `hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ4_XS`, `qwen3.8-iq4-text` | ~1 GB on disk (their weights are shared with `qwen3.8-mates`, which stays) | `ollama create` from `qwen3.8-mates`'s blob |

## Still on disk, waiting

| What | Size | Why it stays for now |
|---|---|---|
| `~/aider-venv/`, `~/.local/state/qwen/aider/` | 666 MB | `bin/qwen` on main still mounts the venv, so `qwen` won't start without it; delete after branch `mates-phase1` merges |
