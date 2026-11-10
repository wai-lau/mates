#!/usr/bin/env python3
"""Test stand-in for the worker/verifier Claude Code. Behaviour from STUB_* env."""
import os, sys, pathlib, json
args = " ".join(sys.argv)
mates = pathlib.Path(".mates")
if "reviewing a proposed change" in args:
    seen = (mates / "report.md").exists()
    v = "fail" if seen else os.environ.get("STUB_VERDICT", "pass")
    (mates / "verdict.md").write_text(f"VERDICT: {v}\n" + ("- saw the worker report\n" if seen else ""))
    print(json.dumps({"result": "ok"})); sys.exit(0)
mode = os.environ.get("STUB_WORKER", "fix")
if mode in ("fix", "noreport"):
    p = pathlib.Path("src/calc.py"); p.write_text(p.read_text().replace("a - b", "a + b"))
if mode == "authorship":
    p = pathlib.Path("src/calc.py"); p.write_text(p.read_text().replace("a - b", "a + b") + "# fixed by Qwen\n")
if mode != "noreport":
    (mates / "report.md").write_text("STATUS: pass\nCHECK-TAIL:\nok\nFILES:\nsrc/calc.py\n")
print(json.dumps({"result": "done"}))
