import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "route_telemetry.py"
spec = importlib.util.spec_from_file_location("route_telemetry", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_aggregate_keeps_only_operational_metadata():
    result = module.aggregate([
        {
            "route": "mixed",
            "block_reason": "none",
            "tool_calls": 4,
            "latency_ms": 120,
            "fallback_used": False,
            "cross_engine_incoherent_attempt": False,
            "query": "private query must disappear",
            "repository_path": "/private/repo",
            "evidence": "private code",
        },
        {
            "route": "block_index",
            "block_reason": "identity_mismatch",
            "tool_calls": 2,
            "latency_ms": 15,
            "fallback_used": True,
            "cross_engine_incoherent_attempt": True,
        },
    ])
    serialized = repr(result)
    assert result["requests"] == 2
    assert result["routes"] == {"block_index": 1, "mixed": 1}
    assert result["cross_engine_incoherent_attempts"] == 1
    assert "private query" not in serialized
    assert "/private/repo" not in serialized
    assert "private code" not in serialized
    assert result["privacy"]["query_text_stored"] is False
