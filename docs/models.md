# Models tried for local coding

Measured on this machine (RTX 5070 Ti + GTX 1660, 39 GB RAM), 2026-11-10,
Ollama 0.40.16, 8-bit KV cache, model spread across both GPUs, one worker.

- **Output tok/s:** generation speed, thinking included (MTP drafts already
  counted).
- **Prompt tok/s:** how fast it reads its input.
- Each speed sample generated only 64 tokens after a long prompt, so treat
  small differences as noise.
- **The probe** is the same small task for every model: fix two bugs in a tiny
  Python module, run in the mates worker sandbox through Claude Code, then
  checked by a blind second Qwen and a rerun of the test.

## Current pick

**`qwen3.8-27b-turbo-heretic:mtp-iq4xs`**, 64K context.

- Source: DavidAU's merge "Qwen3.8-27B-TURBO-Fable-Cold-Fusion-735-882-Heretic-Uncensored-NEO-CODER-MAX", quant `MTP-IQ4_XS` (17.03 GB).
- Uncensored (abliterated with Heretic) and partly trained on Claude Opus outputs, so it may claim to be Claude. It isn't: every request goes to local Ollama, and its sandbox has no network.
- Chosen by Wai for its much shorter thinking. Measured: 40% fewer output tokens than stock Qwen3.8 on the probe.

## Measurements

| Model (Ollama name) | File | Context | On CPU | Output tok/s | Prompt tok/s | Probe |
|---|---|---|---|---|---|---|
| `qwen3.8:27b` (library Q4) | 17 GB | 32K | 5.3 GB | 2.1 | 567 | not run |
| `qwen3.8:27b` (library Q4) | 17 GB | 64K | 7.6 GB | 1.5 | 435 | not run |
| `qwen3.8:27b`, spread + q8 KV | 17 GB | 32K | 4.3 GB | 11.4 | 615 | not run |
| `qwen3.8-mates` (unsloth UD-IQ4_XS, text-only) | 14.25 GB | 32K | 0 | 10.6 | 77 (cold) | |
| `qwen3.8-mates` | 14.25 GB | **64K** | **0** | **19.9** | 161 | pass, 107 s, 2,243 output tokens |
| `qwen3.8-mates` | 14.25 GB | 128K | 1.9 GB | 8.5 | 144 | |
| `qwen3.8-mates`, two workers | 14.25 GB | 32K | 0 | 0.5 | 165 | |
| `qwen3.6:35b-a3b` (MoE, library Q4_K_M) | 21.7 GB | 32K | experts in RAM | 35.5 | 330 | |
| `qwen3.6:35b-a3b` | 21.7 GB | 64K | experts in RAM | 33.9 | 319 | |
| `qwen3.6:35b-a3b` | 21.7 GB | 128K | experts in RAM | 41.1 | 802 | pass, 66 s |
| `qwen3.8-27b-turbo-heretic:mtp-iq4xs` | 17.03 GB | 32K | 1.2 GB | 14.4 | 490 | |
| `qwen3.8-27b-turbo-heretic:mtp-iq4xs` | 17.03 GB | **64K** | 3.3 GB | 8.8 | 729 | pass, 87 s, 1,340 output tokens |

Notes:
- **MoE vs dense.** Ollama places the experts of Qwen3.6-35B-A3B in RAM
  automatically, and with only ~3B parameters active per token it stays fast
  at every context. A dense 27B model halves its speed with even 2 GB spilled.
- **The TURBO model's MTP heads work** in Ollama: 65% of drafted tokens
  accepted, about 3.5 per step.
- **Two workers at once on one dense model is unusable here** (0.4-0.5 tok/s each).
- **Smaller TURBO quant:** `LOW-MTP-IQ4_XS` (15.31 GB) should spill much less
  at 64K. Not yet tried.
- **Claude Code's own system prompt is about 12K tokens.** That is 15-80 s of
  reading at the start of every new session, depending on the model.

## Rebuilding a model

The Modelfiles are in `ollama/`.

- **Library models:** `ollama pull <name>`.
- **A GGUF from Hugging Face:**
  1. Ollama rejects long repo names, so download the file directly:
     `curl -fL -o file.gguf https://huggingface.co/<repo>/resolve/main/<file>.gguf`
  2. Point the `FROM` line of the Modelfile at that file. Keep Ollama's own
     renderer lines: `TEMPLATE {{ .Prompt }}`, `RENDERER qwen3.8` (or `qwen3.5`
     for Qwen3.6), `PARSER qwen3.5`. The GGUF's own Jinja template breaks
     Claude Code.
  3. Drop the vision projector (the second `FROM`) to save about 1 GB of GPU
     memory.
  4. Run `ollama create <name> -f ollama/<file>.Modelfile`.

## Benchmarks that led here

Vendor-reported unless noted.

| Model | Benchmark scores |
|---|---|
| Qwen3.8-27B | LiveCodeBench v6 90.3, SWE-bench Pro 61.7, Terminal-Bench 2.1 73.0 |
| Qwen3.6-35B-A3B | SWE-bench Verified 73.4 |
| Qwen3.6-27B | SWE-bench Verified 77.2 |
| gpt-oss-20b (the old default) | LiveCodeBench 77.7 (Artificial Analysis) |
| TURBO merge | ARC-C 735, ARC-E 882 (fine-tuner's own claims; not a coding benchmark) |
