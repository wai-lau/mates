# Worker environment for Claude Code inside the mates sandbox: KEY=VALUE lines,
# passed as `sandbox.sh --setenv` pairs (blank lines and # comments skipped).
# @NUM_CTX@ is replaced with ctx.json's num_ctx at task start.
# Every model slot points at local Qwen, so no request can name a Claude model.
ANTHROPIC_BASE_URL=http://127.0.0.1:11434
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_MODEL=qwen3.8-mates
ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3.8-mates
ANTHROPIC_DEFAULT_SONNET_MODEL=qwen3.8-mates
ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3.8-mates
ANTHROPIC_DEFAULT_FABLE_MODEL=qwen3.8-mates
ANTHROPIC_SMALL_FAST_MODEL=qwen3.8-mates
CLAUDE_CODE_SUBAGENT_MODEL=qwen3.8-mates
# Unknown model name: run with no per-model client data instead of exiting (2.1.322 binary strings).
CLAUDE_CODE_ALLOW_OTHER_MODELS=1
CLAUDE_CONFIG_DIR=/home/mates/.claude-cfg
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_AUTOUPDATER=1
# Compaction against the real local window, not the 200K assumed for unknown
# models. Name found in the 2.1.322 binary: "To enforce it, set
# CLAUDE_CODE_AUTO_COMPACT_WINDOW=<tokens>". Verified by Task 4 Step 4.
CLAUDE_CODE_AUTO_COMPACT_WINDOW=@NUM_CTX@
# The window itself, for a model name Claude Code does not know (it assumes 200K otherwise).
CLAUDE_CODE_MAX_CONTEXT_TOKENS=@NUM_CTX@
