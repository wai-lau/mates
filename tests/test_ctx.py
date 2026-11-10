import ctx

def r(c, p, cpu): return {"ctx": c, "parallel": p, "cpu_layers": cpu, "vram_mib": [0, 0], "tok_s": 10.0}

def test_picks_largest_gpu_only_ctx_at_one_worker():
    rows = [r(32768, 1, 0), r(65536, 1, 0), r(131072, 1, 3)]
    assert ctx.pick_ctx(rows) == {"num_ctx": 65536, "parallel": 1, "fallback_quant": False}

def test_two_workers_only_if_both_get_64k_gpu_only():
    assert ctx.pick_ctx([r(65536, 1, 0), r(65536, 2, 0)])["parallel"] == 2
    assert ctx.pick_ctx([r(65536, 1, 0), r(65536, 2, 2)])["parallel"] == 1

def test_two_workers_needs_64k_each():
    assert ctx.pick_ctx([r(65536, 1, 0), r(32768, 2, 0)])["parallel"] == 1

def test_below_32k_means_fallback_quant():
    assert ctx.pick_ctx([r(32768, 1, 4)]) == {"num_ctx": None, "parallel": 1, "fallback_quant": True}

def test_caps_at_target_64k_even_if_128k_fits():
    rows = [r(65536, 1, 0), r(131072, 1, 0)]
    assert ctx.pick_ctx(rows)["num_ctx"] == 65536
