import json
import claudejson

def test_parse_event_list():
    out = json.dumps([{"type": "system", "subtype": "init"}, {"type": "assistant"},
                      {"type": "result", "result": "ACCEPT", "usage": {"input_tokens": 2, "output_tokens": 4,
                       "cache_creation_input_tokens": 10, "cache_read_input_tokens": 7}}])
    assert claudejson.parse(out) == {"result": "ACCEPT", "tokens": 16, "cache_read": 7}

def test_parse_single_object():
    out = json.dumps({"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}})
    assert claudejson.parse(out) == {"result": "ok", "tokens": 2, "cache_read": 0}

def test_parse_garbage():
    assert claudejson.parse("Error: boom") == {"result": "", "tokens": 0, "cache_read": 0}
