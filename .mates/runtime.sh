# The runtime mates builds for Qwen panes and checks in this repo (see mates/lead.md, Installs).
# lockfiles:
python3 -m venv "$MATES_RUNTIME/venv"
"$MATES_RUNTIME/venv/bin/pip" install --no-cache-dir pytest==9.2.0 ruff==0.15.11 shellcheck-py==0.10.0.1
echo "PATH=$MATES_RUNTIME/venv/bin" > "$MATES_RUNTIME/env"
