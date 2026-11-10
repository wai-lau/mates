#!/usr/bin/env python3
"""Test stand-in for the lead's `claude -p --output-format json`. Answers from STUB_LEAD (comma list), one per call."""
import os, json, pathlib
seq = os.environ.get("STUB_LEAD", "ACCEPT").split(",")
c = pathlib.Path(os.environ["STUB_LEAD_COUNTER"]); n = int(c.read_text()) if c.exists() else 0
c.write_text(str(n + 1))
print(json.dumps({"result": seq[min(n, len(seq) - 1)].strip(),
                  "usage": {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 50}}))
