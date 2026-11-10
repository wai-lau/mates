#!/usr/bin/env python3
"""Stand-in for a solo Claude session: fixes calc (unless STUB_SOLO=nochange), prints usage JSON."""
import os, json, pathlib
if os.environ.get("STUB_SOLO") != "nochange":
    p = pathlib.Path("src/calc.py"); p.write_text(p.read_text().replace("a - b", "a + b"))
print(json.dumps({"result": "done", "usage": {"input_tokens": 3000, "output_tokens": 400,
                  "cache_creation_input_tokens": 600, "cache_read_input_tokens": 9000}}))
