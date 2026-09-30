import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin-provider", "semantic-memory"))
import mcp_client as mc


def test_rpc_envelope():
    body = mc.rpc_body("tools/list", {}, 7)
    assert body["id"] == 7 and body["method"] == "tools/list" and body["jsonrpc"] == "2.0"


def test_parse_plain():
    assert mc.parse_response('{"jsonrpc":"2.0","id":1,"result":{"ok":1}}') == {"ok": 1}


def test_parse_sse():
    sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":2}}\n'
    assert mc.parse_response(sse) == {"ok": 2}


def test_parse_error_raises():
    try:
        mc.parse_response('{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"x"}}')
        assert False, "should raise"
    except RuntimeError:
        pass


def test_tool_call_payload_shape():
    captured = {}

    class FakeResp:
        def __init__(self, data): self._d = data
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["headers"] = dict(req.headers)
        return FakeResp({"jsonrpc": "2.0", "id": captured["body"]["id"],
                         "result": {"content": [{"type": "text", "text": "{\"results\": []}"}]}})

    mc._urlopen = fake_urlopen
    c = mc.McpClient("http://127.0.0.1:17440/mcp", token="tok")
    c.tool_call("sm_search", {"query": "x"})
    assert captured["body"]["method"] == "tools/call"
    assert captured["body"]["params"]["name"] == "sm_search"
    assert captured["headers"].get("Authorization") == "Bearer tok"
    assert "text/event-stream" in captured["headers"].get("Accept", "")


def test_unwrap_mcp_results_content_text():
    data = {"content": [{"type": "text", "text": json.dumps({"results": [{"id": "1", "score": 0.02}]})}]}
    assert mc.unwrap_results(data) == [{"id": "1", "score": 0.02}]


if __name__ == "__main__":
    ns = dict(globals())
    tests = [(k, v) for k, v in ns.items() if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:
            failed.append(name)
            print("FAIL", name, e)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)