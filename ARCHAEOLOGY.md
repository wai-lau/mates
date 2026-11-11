# Archaeology

What was tried here and then retired, and where the remains are. The live
setup is in [`README.md`](README.md). Local-only leftovers (finished plans,
old chat logs) sit in the gitignored `.archaeology/` folder at the repo root.

## Retired 2026-11-10

| What | Why it went | Where it is now |
|---|---|---|
| aider as the agent behind `qwen aider` | OpenCode became the default (it runs in the same sandbox) and aider fell out of use | code: `bin/qwen` at commit `9a453b2` (the `qwen aider` branch; it leaves `bin/qwen` when branch `mates-phase1` merges); its old chat history and tag cache: `.archaeology/aider/` |
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

## Still on disk, not in use

These were not deleted. Remove them only on purpose:

| Path or model | Size | What |
|---|---|---|
| `~/aider-venv/` | | the aider install |
| `~/.local/state/qwen/aider/` | | aider's state from `qwen aider` |
| `~/.cache/mates-models/turbo-mtp-iq4xs.gguf` | | download copy; Ollama keeps its own |
| `~/.local/opt/llama.cpp-b12257/` | | CUDA llama.cpp build from the gpt-oss era |
| Ollama models `qwen3.8:27b`, `hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ4_XS`, `qwen3.8-iq4-text` | 17 + 15 + 14 GB as listed (they may share blobs) | earlier attempts; see `docs/models.md` |
