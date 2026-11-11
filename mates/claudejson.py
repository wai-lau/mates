"""Parse `claude -p --output-format json`: a list of events (2.1.x) or one result object."""
import json


def parse(stdout: str) -> dict:
    try:
        d = json.loads(stdout)
    except ValueError:
        return {"result": "", "tokens": 0, "cache_read": 0}
    if isinstance(d, list):
        d = next((e for e in reversed(d) if isinstance(e, dict) and e.get("type") == "result"), {})
    u = d.get("usage") or {}
    return {"result": str(d.get("result", "")).strip(),
            "tokens": int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
            + int(u.get("cache_creation_input_tokens", 0)),
            "cache_read": int(u.get("cache_read_input_tokens", 0))}
