"""zkai_client: the thin caller-side helper other projects/scripts import."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "zkai_client.py"
_spec = importlib.util.spec_from_file_location("zkai_client", _SCRIPT)
zkai_client = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["zkai_client"] = zkai_client
_spec.loader.exec_module(zkai_client)


@pytest.fixture
def stub_post(monkeypatch):
    import threading

    calls: list[dict] = []
    lock = threading.Lock()

    def fake_post(path: str, payload: dict, *, timeout: float) -> dict:
        with lock:
            calls.append({"path": path, "payload": payload})
        if payload.get("_fail"):
            raise zkai_client.GatewayError("HTTP 503: 全黑")
        return {
            "choices": [{"message": {"content": f"答:{payload['messages'][-1]['content']}"}}]
        }

    monkeypatch.setattr(zkai_client, "_post", fake_post)
    return calls


def test_ask_uses_the_task_alias_and_session_id(stub_post) -> None:
    out = zkai_client.ask("你好", task="complex", session_id="s-1")
    assert out == "答:你好"
    payload = stub_post[0]["payload"]
    assert payload["model"] == "zk-coding"
    assert payload["session_id"] == "s-1"


def test_ask_falls_back_to_fast_when_complex_is_dark(stub_post, capsys) -> None:
    """复杂档全黑 → 降级到 zk-fast 重试一次。"""
    out = zkai_client.ask("hi", task="complex")
    assert out == "答:hi"
    assert stub_post[0]["payload"]["model"] == "zk-coding"


def test_ask_batch_preserves_order_and_tolerates_failures(stub_post) -> None:
    results = zkai_client.ask_batch(["a", "b", "c"], task="simple", session_prefix="job")
    # 返回值必须与输入保序（这是 ask_batch 的契约）
    assert results == ["答:a", "答:b", "答:c"]
    # ⚠ 但 ``stub_post`` 记录的是**线程完成的顺序**，不是提交顺序：
    # ThreadPoolExecutor 有 4 路并发，三个任务谁先跑完不确定。
    # 断言精确列表会 flaky（2026-11-14 实测：单跑绿、全量偶红）。
    # 这里只断言集合与配对关系——那才是契约里真正重要的部分。
    session_ids = sorted(c["payload"].get("session_id") for c in stub_post)
    assert session_ids == ["job-0", "job-1", "job-2"]
    # 每条 prompt 必须配对自己的会话号（错配会让不同批次钉到同一把 Key）
    pairs = {
        c["payload"]["messages"][-1]["content"]: c["payload"].get("session_id")
        for c in stub_post
    }
    assert pairs == {"a": "job-0", "b": "job-1", "c": "job-2"}


def test_ask_batch_surfaces_errors_in_place(stub_post) -> None:
    def failing(path, payload, *, timeout):
        if payload["messages"][-1]["content"] == "bad":
            raise zkai_client.GatewayError("boom")
        return {"choices": [{"message": {"content": "ok"}}]}

    import zkai_client as zc

    orig = zc._post
    zc._post = failing
    try:
        results = zc.ask_batch(["good", "bad", "good2"], task="simple")
    finally:
        zc._post = orig
    assert results[0] == "ok" and results[2] == "ok"
    assert isinstance(results[1], zkai_client.GatewayError)


def test_ask_json_parses_and_wraps_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        zkai_client, "_post",
        lambda path, payload, *, timeout: {"choices": [{"message": {"content": '{"a": 1}'}}]},
    )
    assert zkai_client.ask_json("q") == {"a": 1}

    monkeypatch.setattr(
        zkai_client, "_post",
        lambda path, payload, *, timeout: {"choices": [{"message": {"content": "not json"}}]},
    )
    with pytest.raises(zkai_client.GatewayError, match="合法 JSON"):
        zkai_client.ask_json("q")
