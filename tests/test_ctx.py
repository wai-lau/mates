import ctx

def r(c, p, cpu): return {"ctx": c, "parallel": p, "cpu_layers": cpu, "vram_mib": [0, 0], "tok_s": 10.0}

def test_picks_largest_gpu_only_ctx_at_one_worker():
    rows = [r(32768, 1, 0), r(65536, 1, 0), r(131072, 1, 3)]
    assert ctx.pick_ctx(rows) == {"num_ctx": 65536, "parallel": 1, "fallback_quant": False, "below_agent_floor": False}

def test_two_workers_only_if_both_get_64k_gpu_only():
    assert ctx.pick_ctx([r(65536, 1, 0), r(65536, 2, 0)])["parallel"] == 2
    assert ctx.pick_ctx([r(65536, 1, 0), r(65536, 2, 2)])["parallel"] == 1

def test_two_workers_needs_64k_each():
    assert ctx.pick_ctx([r(65536, 1, 0), r(32768, 2, 0)])["parallel"] == 1

def test_below_32k_means_fallback_quant():
    assert ctx.pick_ctx([r(32768, 1, 4)]) == {"num_ctx": None, "parallel": 1, "fallback_quant": True, "below_agent_floor": True}

def test_no_cap_takes_largest_gpu_only():
    # Ollama: 64K is the floor for agents; "use the maximum context length ... avoid offloading to CPU"
    rows = [r(65536, 1, 0), r(131072, 1, 0)]
    assert ctx.pick_ctx(rows)["num_ctx"] == 131072

def test_between_32k_and_64k_is_usable_but_flagged():
    assert ctx.pick_ctx([r(32768, 1, 0), r(65536, 1, 2)]) == {"num_ctx": 32768, "parallel": 1,
                                                              "fallback_quant": False, "below_agent_floor": True}
