#!/usr/bin/env python3
"""Measure Qwen context sizes on this GPU pair and pick the worker context.

    python3 mates/ctx.py measure [--model qwen3.8:27b]

Runs a private `ollama serve` as wai on 127.0.0.1:11435 (the pilot's only
Ollama; the system service is left alone), and for each context size and
worker count fills ~90% of the context, then records CPU spill, VRAM per
GPU and speed. Writes ~/.claude/mates/ctx-rows.json and ctx.json.
"""
import argparse
import concurrent.futures
import json
import os
import pathlib
import subprocess
import time
import urllib.request

TARGET, FLOOR = 65536, 32768
CTXS, PARALLELS = (32768, 65536), (1, 2)  # 128K dropped: the pick caps at 64K, and at ~160 prompt tok/s it costs ~25 min
PORT = 11435
STATE = pathlib.Path.home() / ".claude" / "mates"


def pick_ctx(rows: list[dict]) -> dict:
    """Largest GPU-only context at one worker, capped at the 64K target.

    Two workers only if both slots get >= 64K with nothing on CPU. Under the
    32K floor: no context, use the fallback quant.
    """
    gpu_only = [r for r in rows if r["cpu_layers"] == 0]
    one = [r["ctx"] for r in gpu_only if r["parallel"] == 1 and r["ctx"] >= FLOOR]
    if not one:
        return {"num_ctx": None, "parallel": 1, "fallback_quant": True}
    num_ctx = min(max(one), TARGET)
    two = any(r["parallel"] == 2 and r["ctx"] >= TARGET for r in gpu_only)
    return {"num_ctx": num_ctx, "parallel": 2 if two else 1, "fallback_quant": False}


def _api(path, body=None, timeout=1800):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _serve(parallel: int, log):
    env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{PORT}", "OLLAMA_MODELS": "/usr/share/ollama/.ollama/models",
           "OLLAMA_NOPRUNE": "1", "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KEEP_ALIVE": "10m",
           "OLLAMA_NUM_PARALLEL": str(parallel), "OLLAMA_MAX_LOADED_MODELS": "1"}
    p = subprocess.Popen(["ollama", "serve"], env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            _api("/api/version", timeout=2)
            return p
        except OSError:
            time.sleep(1)
    p.kill()
    raise RuntimeError("private ollama did not start")


def _vram():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return [int(x) for x in out.split()]


def _one(model, ctx):
    words = int(ctx * 0.9 * 0.75)  # ~0.75 words per token for plain English filler
    prompt = ("the quick brown fox jumps over the lazy dog " * (words // 9 + 1)) + "\nSummarise in one line."
    r = _api("/api/generate", {"model": model, "prompt": prompt, "stream": False,
                               "options": {"num_ctx": ctx, "num_predict": 64}})
    return {"prompt_tokens": r.get("prompt_eval_count", 0),
            "prompt_tok_s": r.get("prompt_eval_count", 0) / max(r.get("prompt_eval_duration", 1) / 1e9, 1e-9),
            "tok_s": r.get("eval_count", 0) / max(r.get("eval_duration", 1) / 1e9, 1e-9)}


def measure(model: str) -> list[dict]:
    subprocess.run(["ollama", "stop", "gpt-oss:20b"], capture_output=True)
    STATE.mkdir(parents=True, exist_ok=True)
    rows = []
    for parallel in PARALLELS:
        with open(STATE / f"ctx-serve-p{parallel}.log", "w") as log:
            srv = _serve(parallel, log)
            try:
                for c in CTXS:
                    t0 = time.time()
                    try:
                        with concurrent.futures.ThreadPoolExecutor(parallel) as ex:
                            res = list(ex.map(lambda _: _one(model, c), range(parallel)))
                    except OSError as e:
                        rows.append({"ctx": c, "parallel": parallel, "cpu_layers": -1, "vram_mib": _vram(),
                                     "tok_s": 0.0, "error": repr(e)})
                        continue
                    ps = _api("/api/ps")["models"]
                    m = ps[0] if ps else {}
                    spill = max(m.get("size", 0) - m.get("size_vram", 0), 0)
                    rows.append({"ctx": c, "parallel": parallel, "cpu_layers": 0 if spill == 0 else 1,
                                 "cpu_bytes": spill, "model_bytes": m.get("size", 0), "vram_mib": _vram(),
                                 "tok_s": round(min(x["tok_s"] for x in res), 1),
                                 "prompt_tok_s": round(min(x["prompt_tok_s"] for x in res), 1),
                                 "prompt_tokens": res[0]["prompt_tokens"], "wall_s": round(time.time() - t0, 1)})
                    print(json.dumps(rows[-1]), flush=True)
                    _api("/api/generate", {"model": model, "keep_alive": 0})  # unload: next ctx loads fresh
            finally:
                srv.terminate()
                srv.wait(30)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["measure"])
    ap.add_argument("--model", default="qwen3.8:27b")
    a = ap.parse_args()
    rows = measure(a.model)
    (STATE / "ctx-rows.json").write_text(json.dumps(rows, indent=1))
    choice = {**pick_ctx(rows), "model": a.model}
    (STATE / "ctx.json").write_text(json.dumps(choice, indent=1))
    print("ctx | workers | on CPU | VRAM MiB (5070Ti, 1660) | gen tok/s | prompt tok/s")
    for r in rows:
        print(f"{r['ctx']:>6} | {r['parallel']} | {r.get('cpu_bytes', '?')} | {r['vram_mib']} | "
              f"{r['tok_s']} | {r.get('prompt_tok_s', '-')}")
    print("choice:", json.dumps(choice))


if __name__ == "__main__":
    main()
