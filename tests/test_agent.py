"""ZK-Agent 离线测试：脚本化适配器模拟「先调工具、后给终答」的两轮循环。

覆盖：工具执行与结果回填、write_file 审批（批准/拒绝/本会话记忆）、
路径逃逸拒绝、命令黑名单、max_steps 熔断、会话持久化与继续、API 面。
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.errors import UpstreamServerError
from app.main import create_app
from app.models.request import ChatCompletionRequest
from app.models.response import ChatCompletionResponse, Usage
from app.providers.base import ProviderContext
from app.services.agent.tools import ToolBox, ToolError
from tests.conftest import (
    FakeAdapter,
    build_harness,
    make_alias,
    make_config,
    make_model,
    make_provider,
)

# --------------------------------------------------------------------------- #
# Scripted adapter: returns queued responses verbatim
# --------------------------------------------------------------------------- #


class AgentFakeAdapter(FakeAdapter):
    """chat() 按脚本逐个返回预构造的响应对象（支持 tool_calls）。"""

    def __init__(self, config, *, default: ChatCompletionResponse | None = None) -> None:
        super().__init__(config)
        self.responses: deque[ChatCompletionResponse] = deque()
        self.default_response = default
        self.requests: list[ChatCompletionRequest] = []

    def queue(self, *responses: ChatCompletionResponse) -> AgentFakeAdapter:
        self.responses.extend(responses)
        return self

    async def chat(self, request: ChatCompletionRequest, ctx: ProviderContext):
        self._record(ctx, stream=False)
        self.build_payload(request, ctx.deployment)
        self.requests.append(request)
        if self.responses:
            return self.responses.popleft()
        if self.default_response is not None:
            return self.default_response
        return ChatCompletionResponse.simple(model=ctx.upstream_model, content="ok",
                                             usage=Usage.build(10, 5))


def tool_resp(*calls: tuple[str, dict[str, Any]]) -> ChatCompletionResponse:
    """构造一条只含 tool_calls 的助手响应。"""
    from app.models.request import FunctionCall, ToolCall

    tcs = [
        ToolCall(id=f"call_{i}", function=FunctionCall(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(calls)
    ]
    return ChatCompletionResponse.simple(model="fake-model", content="",
                                         tool_calls=tcs, usage=Usage.build(30, 15))


def text_resp(text: str) -> ChatCompletionResponse:
    return ChatCompletionResponse.simple(model="fake-model", content=text,
                                         usage=Usage.build(40, 20))


async def wait_for_status(service, sid: str, statuses: set[str], max_wait: float = 8.0) -> dict:
    """轮询直到会话进入目标状态（循环跑在后台任务里）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        row = await service.repo.get_session(sid)
        if row is not None and row["status"] in statuses:
            return row
        await asyncio.sleep(0.02)
    row = await service.repo.get_session(sid)
    raise AssertionError(f"session {sid} stuck at {row!r}, wanted {statuses}")


def agent_config(tmp_path: Path, *, max_steps: int = 40):
    config = make_config(
        providers=[make_provider("fake", key_ids=("key-1",))],
        models=[make_model("fake-model", capabilities={"tool_use": 9.0})],
        aliases=[make_alias("zk-auto", ["fake-model"])],
    )
    config.settings.agent_workspace = tmp_path / "ws"
    config.settings.agent_max_steps = max_steps
    config.settings.agent_context_token_limit = 1_000_000
    return config


async def start_session(harness, adapter, *, task: str = "做个任务") -> str:
    service = harness.container.agent_service
    created = await service.start(task=task)
    return created["session"]["id"]


async def decide_approval(service, sid: str) -> str | None:
    """找到第一个 pending 的审批 id。"""
    run = service._running.get(sid)
    if run is None:
        return None
    return next(iter(run.approvals.pending), None)


# --------------------------------------------------------------------------- #
# 子任务派发（spawn_subagent）
# --------------------------------------------------------------------------- #


def spawn_config(tmp_path: Path, *, max_steps: int = 40, depth: int = 2,
                 live_children: int = 2, total_children: int = 4):
    """父子分**不同 provider** 的配置。

    为什么必须分：``AgentFakeAdapter`` 只有一个 deque，而 adapter 按 provider
    注册——父子共用一个实例时会争抢同一条队列，父的 spawn 响应可能被子 pop 走，
    时序上不可预测。分成 fake / fake-child 两家，各自的响应队列就独立了。
    角色表也按此把 vision 指向 child 那边，父的 zk-auto 留在 fake 上。
    """
    config = make_config(
        providers=[
            make_provider("fake", key_ids=("key-1",)),
            make_provider("fake-child", key_ids=("key-2",)),
        ],
        models=[
            make_model("fake-model", provider_id="fake", capabilities={"tool_use": 9.0}),
            make_model("fake-child-model", provider_id="fake-child",
                       capabilities={"tool_use": 9.0, "vision": 9.0}),
        ],
        aliases=[
            make_alias("zk-auto", ["fake-model"]),
            # 带硬门槛的 vision 别名：vision 角色派过来时走这条链。
            make_alias("zk-vision", ["fake-child-model"], requires={"vision": 8.0},
                       weights={"vision": 5.0}),
        ],
    )
    config.settings.agent_workspace = tmp_path / "ws"
    config.settings.agent_max_steps = max_steps
    config.settings.agent_context_token_limit = 1_000_000
    config.settings.agent_max_depth = depth
    config.settings.agent_max_live_children = live_children
    config.settings.agent_max_children_total = total_children
    return config


def spawn_harness(tmp_path: Path, **kwargs):
    """父 adapter（fake）+ 子 adapter（fake-child）的 harness。"""
    parent_adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",)))
    child_adapter = AgentFakeAdapter(make_provider("fake-child", key_ids=("key-2",)))
    config = spawn_config(tmp_path, **kwargs)
    return config, parent_adapter, child_adapter


# --------------------------------------------------------------------------- #
# Loop behaviour
# --------------------------------------------------------------------------- #


async def test_read_file_two_step_cycle(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("第一行\n第二行", encoding="utf-8")
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        tool_resp(("read_file", {"path": "hello.txt"})),
        text_resp("读完啦：内容是两行"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    sid = await start_session(harness, adapter)

    service = harness.container.agent_service
    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    assert row["steps"] == 2
    assert row["input_tokens"] == 70 and row["output_tokens"] == 35

    messages = await service.repo.messages(sid)
    tool_rows = [m for m in messages if m["kind"] == "tool_result"]
    assert len(tool_rows) == 1
    assert "第二行" in tool_rows[0]["content"]
    assert tool_rows[0]["data"]["tool_call_id"].startswith("call_")
    # 第二轮请求应包含 tool 消息回填
    assert any(m.role == "tool" for m in adapter.requests[1].messages)
    await harness.container.shutdown()


async def test_write_file_requires_approval_then_writes(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        tool_resp(("write_file", {"path": "new.txt", "content": "abc"})),
        text_resp("写好了"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    row = await wait_for_status(service, sid, {"waiting_approval"})
    approval_id = await decide_approval(service, sid)
    assert approval_id
    # 未批准前文件不存在
    assert not (tmp_path / "ws" / "new.txt").exists()
    await service.decide(sid, approval_id, approved=True, remember=False)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    assert (tmp_path / "ws" / "new.txt").read_text(encoding="utf-8") == "abc"
    messages = await service.repo.messages(sid)
    assert any(m["kind"] == "approval" and m["data"].get("decision") is True for m in messages)
    await harness.container.shutdown()


async def test_write_file_denied(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        tool_resp(("write_file", {"path": "nope.txt", "content": "x"})),
        text_resp("好的，不写了"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    await wait_for_status(service, sid, {"waiting_approval"})
    approval_id = await decide_approval(service, sid)
    await service.decide(sid, approval_id, approved=False, remember=False)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    assert not (tmp_path / "ws" / "nope.txt").exists()
    messages = await service.repo.messages(sid)
    tool_rows = [m for m in messages if m["kind"] == "tool_result"]
    assert "拒绝" in tool_rows[0]["content"]
    await harness.container.shutdown()


async def test_remember_skips_second_approval(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        tool_resp(("write_file", {"path": "a.txt", "content": "1"})),
        tool_resp(("write_file", {"path": "b.txt", "content": "2"})),
        text_resp("两件都完成"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    await wait_for_status(service, sid, {"waiting_approval"})
    approval_id = await decide_approval(service, sid)
    await service.decide(sid, approval_id, approved=True, remember=True)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    # 第二个写操作带着「不再询问」直接落盘，没再进入 waiting_approval
    assert (tmp_path / "ws" / "a.txt").exists()
    assert (tmp_path / "ws" / "b.txt").exists()
    # 审批请求只有一次（另有一行决策记录）
    messages = await service.repo.messages(sid)
    requests = [m for m in messages
                if m["kind"] == "approval" and m["data"].get("decision") is None]
    assert len(requests) == 1
    await harness.container.shutdown()


async def test_path_escape_rejected(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        tool_resp(("read_file", {"path": "../../.env"})),
        text_resp("了解"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    messages = await service.repo.messages(sid)
    tool_rows = [m for m in messages if m["kind"] == "tool_result"]
    assert tool_rows[0]["data"]["ok"] is False
    assert "越出工作区" in tool_rows[0]["content"]
    await harness.container.shutdown()


async def test_command_blocklist(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        tool_resp(("run_command", {"command": "rm -rf /"})),
        text_resp("好的，换个方案"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    # 黑名单命中不该进入等待审批，直接把失败喂回模型
    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    messages = await service.repo.messages(sid)
    tool_rows = [m for m in messages if m["kind"] == "tool_result"]
    assert "黑名单" in tool_rows[0]["content"]
    await harness.container.shutdown()


async def test_run_command_after_approval(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        tool_resp(("run_command", {"command": "echo zk-agent-ok"})),
        text_resp("命令跑完"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    await wait_for_status(service, sid, {"waiting_approval"})
    approval_id = await decide_approval(service, sid)
    await service.decide(sid, approval_id, approved=True, remember=False)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    messages = await service.repo.messages(sid)
    tool_rows = [m for m in messages if m["kind"] == "tool_result"]
    assert tool_rows[0]["data"]["ok"] is True
    assert "zk-agent-ok" in tool_rows[0]["content"]
    await harness.container.shutdown()


async def test_max_steps_break_loop(tmp_path):
    adapter = AgentFakeAdapter(
        make_provider("fake", key_ids=("key-1",)),
        default=tool_resp(("list_dir", {"path": "."})),
    )
    harness = await build_harness(agent_config(tmp_path, max_steps=2), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    assert row["steps"] == 2
    messages = await service.repo.messages(sid)
    assert any(m["kind"] == "note" and "步数上限" in (m["content"] or "") for m in messages)
    await harness.container.shutdown()


async def test_followup_replays_history(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        text_resp("第一轮完成"),
        text_resp("第二轮完成"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter, task="任务一")

    await wait_for_status(service, sid, {"done", "failed"})
    detail = await service.followup(sid, "再干一件事")
    assert detail is not None
    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done"
    # 第二轮请求重放了第一轮的历史
    second = adapter.requests[1]
    texts = [m.text() for m in second.messages]
    assert "任务一" in texts
    assert "第一轮完成" in texts
    assert "再干一件事" in texts
    await harness.container.shutdown()


async def test_zombie_session_marked_interrupted(tmp_path):
    harness = await build_harness(agent_config(tmp_path),
                                  adapters={"fake": AgentFakeAdapter(make_provider("fake"))})
    service = harness.container.agent_service
    await service.repo.create_session(session_id="zombie", title="遗留", model="zk-auto",
                                      workspace=str(tmp_path))
    sessions = await service.list_sessions()
    assert sessions[0]["status"] == "interrupted"
    detail = await service.session_detail("zombie")
    assert detail["session"]["status"] == "interrupted"
    await harness.container.shutdown()


async def test_llm_failure_marks_session_failed(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",)))

    async def boom(request, ctx):
        adapter._record(ctx, stream=False)
        raise UpstreamServerError("上游全挂", provider="fake", model="fake-model")

    adapter.chat = boom  # type: ignore[method-assign]
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    service = harness.container.agent_service
    sid = await start_session(harness, adapter)

    row = await wait_for_status(service, sid, {"failed"})
    assert "上游全挂" in (row["error"] or "")
    await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# ToolBox unit tests
# --------------------------------------------------------------------------- #


def _run(coro):
    """同步测试里跑协程（pytest-asyncio 只接管 async 用例）。"""
    return asyncio.run(coro)


def test_toolbox_read_and_grep(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (ws / ".venv").mkdir()
    (ws / ".venv" / "b.py").write_text("should be skipped", encoding="utf-8")
    box = ToolBox(ws)

    out = _run(box.perform("read_file", {"path": "a.py"}))
    assert "def f():" in out.output

    out = _run(box.perform("grep", {"pattern": "return", "path": "."}))
    assert "a.py:2" in out.output
    assert "b.py" not in out.output

    with pytest.raises(ToolError, match="越出工作区"):
        box.resolve("..")


def test_toolbox_write_diff_and_modes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    box = ToolBox(ws)
    target = ws / "f.txt"
    target.write_text("old\n", encoding="utf-8")

    prepared = box._prepare_write({"path": "f.txt", "content": "new\n"})
    assert prepared.needs_approval
    assert "-old" in prepared.display["diff"] and "+new" in prepared.display["diff"]

    _run(box.perform("write_file", {"path": "f.txt", "content": "new\n"}))
    assert target.read_text(encoding="utf-8") == "new\n"

    _run(box.perform("write_file", {"path": "f.txt", "content": "app\n", "mode": "append"}))
    assert target.read_text(encoding="utf-8") == "new\napp\n"


def test_toolbox_command_blocklist_patterns(tmp_path):
    box = ToolBox(tmp_path / "ws")
    for bad in ("rm -rf /tmp", "git push origin main", "git reset --hard HEAD~1",
                "format C:", "shutdown /s", "Remove-Item -Recurse -Force x"):
        with pytest.raises(ToolError, match="黑名单"):
            box._prepare_command({"command": bad})
    prepared = box._prepare_command({"command": "echo hi"})
    assert prepared.needs_approval


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


async def test_api_agent_endpoints(tmp_path):
    adapter = AgentFakeAdapter(make_provider("fake", key_ids=("key-1",))).queue(
        text_resp("done via api"),
    )
    harness = await build_harness(agent_config(tmp_path), adapters={"fake": adapter})
    settings = harness.container.settings
    app = create_app(settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/admin/agent/models")
        assert r.status_code == 200
        assert "zk-auto" in r.json()["aliases"]

        r = await client.post("/admin/agent/sessions",
                              json={"task": "x", "model": "unknown-model"})
        assert r.status_code == 400

        r = await client.post("/admin/agent/sessions", json={"task": "api 任务"})
        assert r.status_code == 201
        sid = r.json()["session"]["id"]
        row = await wait_for_status(harness.container.agent_service, sid, {"done", "failed"})
        assert row["status"] == "done"

        r = await client.get(f"/admin/agent/sessions/{sid}")
        assert r.status_code == 200
        kinds = [m["kind"] for m in r.json()["messages"]]
        assert "task" in kinds and "message" in kinds

        # SSE：终态会话应立刻回 snapshot + session_done
        r = await client.get(f"/admin/agent/sessions/{sid}/events")
        assert r.status_code == 200
        assert "snapshot" in r.text and "session_done" in r.text

        r = await client.delete(f"/admin/agent/sessions/{sid}")
        assert r.status_code == 200
        r = await client.get(f"/admin/agent/sessions/{sid}")
        assert r.status_code == 404
    await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# spawn_subagent：派发成功
# --------------------------------------------------------------------------- #


async def test_spawn_child_completes_and_reports_back(tmp_path: Path) -> None:
    """父派一个 vision 子任务，子的结论要回填进父的 tool_result。"""
    config, parent_adapter, child_adapter = spawn_harness(tmp_path)
    parent_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "vision", "task": "看这张图，说出颜色"})),
        text_resp("子任务说主要颜色是蓝色，我据此继续。"),
    )
    child_adapter.queue(text_resp("图里的主要颜色是蓝色。"))
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done", row["error"]

    messages = await service.repo.messages(sid)
    tool_rows = [m for m in messages if m["kind"] == "tool_result"
                 and (m.get("data") or {}).get("tool") == "spawn_subagent"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["data"]["ok"] is True
    assert "图里的主要颜色是蓝色" in tool_rows[0]["content"]

    # 父的下一步请求里必须带着这条 tool_result（OpenAI 协议要求配对）
    last_request = parent_adapter.requests[-1]
    assert any(m.role == "tool" and "子任务" in (m.content or "") for m in last_request.messages)

    # 子会话自己是独立的会话，不是父 transcript 里的几行
    child_ids = [r["id"] for r in await service.repo.list_sessions()
                 if r["id"] != sid]
    assert len(child_ids) == 1
    child_messages = await service.repo.messages(child_ids[0])
    assert child_messages[0]["content"] == "看这张图，说出颜色"
    await harness.container.shutdown()


async def test_unknown_role_returns_the_role_list(tmp_path: Path) -> None:
    """角色不在注册表 -> ok=False + 可用清单，父会话本身不能失败。"""
    config, parent_adapter, child_adapter = spawn_harness(tmp_path)
    parent_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "厨师", "task": "炒个菜"})),
        text_resp("没有这个角色，我自己来。"),
    )
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done", row["error"]

    tool_rows = [m for m in await service.repo.messages(sid)
                 if m["kind"] == "tool_result"
                 and (m.get("data") or {}).get("tool") == "spawn_subagent"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["data"]["ok"] is False
    output = tool_rows[0]["content"]
    assert "厨师" in output and "vision" in output and "coder" in output
    # 不该真的起一个子会话
    assert len(await service.repo.list_sessions()) == 1
    await harness.container.shutdown()


async def test_child_cannot_spawn_grandchild(tmp_path: Path) -> None:
    """子的工具箱里没有 spawn_subagent——模型硬调也拿不到这个 schema。"""
    config, parent_adapter, child_adapter = spawn_harness(tmp_path)
    parent_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "vision", "task": "看图"})),
        text_resp("收到。"),
    )
    # 子不服气，硬要再派一级
    child_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "vision", "task": "再派一层"})),
        text_resp("派不动，我自己看完图了。"),
    )
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done", row["error"]

    # 子请求里根本没有 spawn_subagent 这个 schema
    assert child_adapter.requests, "子会话应该发过请求"
    for request in child_adapter.requests:
        assert request.tools is not None
        assert "spawn_subagent" not in {t["function"]["name"] for t in request.tools}

    # 全局只有父子两个会话，没有孙辈
    assert len(await service.repo.list_sessions()) == 2
    await harness.container.shutdown()


async def test_child_failure_does_not_kill_the_parent(tmp_path: Path) -> None:
    """子会话炸了，父拿到 ok=False 的说明，仍能自己收尾。"""
    config, parent_adapter, child_adapter = spawn_harness(tmp_path)
    parent_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "vision", "task": "看图"})),
        text_resp("子任务失败了，我自己处理。"),
    )

    async def boom(request, ctx):
        raise UpstreamServerError("上游全挂", provider="fake-child", model="fake-child-model")

    child_adapter.chat = boom  # type: ignore[method-assign]

    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done", row["error"]

    tool_rows = [m for m in await service.repo.messages(sid)
                 if m["kind"] == "tool_result"
                 and (m.get("data") or {}).get("tool") == "spawn_subagent"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["data"]["ok"] is False
    assert "上游全挂" in tool_rows[0]["content"]
    await harness.container.shutdown()


async def test_live_children_limit_refuses_a_second_child(tmp_path: Path) -> None:
    """同一轮里并行派两个子任务，第二个该被「并发已满」拒绝。"""
    config, parent_adapter, child_adapter = spawn_harness(tmp_path, live_children=1)
    # 父一次性吐出两个 spawn 调用
    parent_adapter.queue(
        tool_resp(
            ("spawn_subagent", {"role": "vision", "task": "第一张图"}),
            ("spawn_subagent", {"role": "vision", "task": "第二张图"}),
        ),
        text_resp("一个成功一个被拒，我按成功的那个继续。"),
    )
    child_adapter.queue(text_resp("第一张图是蓝色的。"))
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done", row["error"]

    tool_rows = [m for m in await service.repo.messages(sid)
                 if m["kind"] == "tool_result"
                 and (m.get("data") or {}).get("tool") == "spawn_subagent"]
    assert len(tool_rows) == 2
    verdicts = sorted(str(r["data"]["ok"]) for r in tool_rows)
    assert verdicts == ["False", "True"], verdicts
    refused = next(r for r in tool_rows if r["data"]["ok"] is False)
    assert "并发已满" in refused["content"] or "上限" in refused["content"]
    await harness.container.shutdown()


async def test_depth_gate_refuses_deeper_dispatch(tmp_path: Path) -> None:
    """depth 到上限时直接拒绝，且不能把父会话拖成 failed。"""
    config, parent_adapter, child_adapter = spawn_harness(tmp_path, depth=1)
    parent_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "vision", "task": "看图"})),
        text_resp("派不动，我自己来。"),
    )
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done", row["error"]

    tool_rows = [m for m in await service.repo.messages(sid)
                 if m["kind"] == "tool_result"
                 and (m.get("data") or {}).get("tool") == "spawn_subagent"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["data"]["ok"] is False
    assert "深度" in tool_rows[0]["content"]
    assert len(await service.repo.list_sessions()) == 1  # 没起子会话
    await harness.container.shutdown()


async def test_children_do_not_steal_the_root_semaphore(tmp_path: Path) -> None:
    """死锁回归：max_concurrent=1 时根会话派出的子任务仍要能跑起来。

    这是本次最容易踩的坑。根 loop 持着 ``_sem`` 等子，子若也来抢 ``_sem``，
    在 max_concurrent=1 下就是「持锁等锁」——父子互等，两个会话永远不结束。
    子走独立的 ``_child_sem``，所以这里即使只给一个根槽，子也该跑完。
    """
    config, parent_adapter, child_adapter = spawn_harness(tmp_path)
    config.settings.agent_max_concurrent = 1
    parent_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "vision", "task": "看图"})),
        text_resp("子任务回来了，我收尾。"),
    )
    child_adapter.queue(text_resp("图是蓝的。"))
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)

    row = await wait_for_status(service, sid, {"done", "failed"}, max_wait=10)
    assert row["status"] == "done", row["error"]

    tool_rows = [m for m in await service.repo.messages(sid)
                 if m["kind"] == "tool_result"
                 and (m.get("data") or {}).get("tool") == "spawn_subagent"]
    assert tool_rows and tool_rows[0]["data"]["ok"] is True
    assert "图是蓝的" in tool_rows[0]["content"]
    await harness.container.shutdown()


async def test_child_events_reach_the_parent_subscribers(tmp_path: Path) -> None:
    """子会话的进度要透传到父的 SSE 订阅者，否则父页面看不出子在干什么。"""
    config, parent_adapter, child_adapter = spawn_harness(tmp_path)
    parent_adapter.queue(
        tool_resp(("spawn_subagent", {"role": "vision", "task": "看图"})),
        text_resp("收到。"),
    )
    child_adapter.queue(text_resp("蓝"))
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    sid = await start_session(harness, parent_adapter)
    queue = await service.subscribe(sid)

    row = await wait_for_status(service, sid, {"done", "failed"})
    assert row["status"] == "done", row["error"]

    child_events: list[dict[str, Any]] = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.get("type") == "child_event":
            child_events.append(event)
    assert child_events, "父订阅者应该收到子会话的事件"
    # 透传的事件必须带 child_session，前端才知道这行属于哪个子任务
    assert all(e.get("child_session") for e in child_events)
    service.unsubscribe(sid, queue)
    await harness.container.shutdown()


async def test_spawn_capacity_is_independent_of_root_capacity(tmp_path: Path) -> None:
    """子任务容量与根容量是两个独立信号量，互不挤占。"""
    from app.services.agent.service import AgentService

    config, parent_adapter, child_adapter = spawn_harness(tmp_path, total_children=4)
    harness = await build_harness(config, adapters={
        "fake": parent_adapter, "fake-child": child_adapter,
    })
    service = harness.container.agent_service
    # 根 3 席、子 4 席，是两个不同的对象
    assert service._sem._value == 3  # type: ignore[attr-defined]
    assert service._child_sem._value == 4  # type: ignore[attr-defined]
    assert service._sem is not service._child_sem
    assert isinstance(service, AgentService)
    await harness.container.shutdown()
