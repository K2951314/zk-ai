"""ORM models.

``providers``, ``models``, ``credentials``, ``deployments``, ``model_aliases``,
``requests``, ``request_attempts``, ``health_checks``, ``usage_records`` plus the
ZK-Agent tables ``agent_sessions`` / ``agent_messages`` and the automation-hub
table ``events``.

Security note: **no table stores a secret**. ``credentials`` only keeps a
reference to the environment variable (``secret_ref``) plus a non-reversible
fingerprint, which is enough for de-duplication and debugging.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for every table."""


def utcnow() -> dt.datetime:
    """Timezone-aware UTC now (SQLite stores it naive, reads it back naive)."""
    return dt.datetime.now(dt.UTC)


def to_datetime(epoch: float | None) -> dt.datetime | None:
    """Convert a ``time.time()`` value into a datetime for storage."""
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(epoch, tz=dt.UTC)


# --------------------------------------------------------------------------- #
# Configuration mirror (YAML -> database, for admin queries & joins)
# --------------------------------------------------------------------------- #
class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    credentials: Mapped[list[Credential]] = relationship(
        back_populates="provider", cascade="all, delete-orphan",
        lazy="selectin", passive_deletes=True,
    )
    deployments: Mapped[list[Deployment]] = relationship(
        back_populates="provider", cascade="all, delete-orphan",
        lazy="selectin", passive_deletes=True,
    )


class Model(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    owned_by: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    context_window: Mapped[int] = mapped_column(Integer, default=128_000, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    deployments: Mapped[list[Deployment]] = relationship(
        back_populates="model", cascade="all, delete-orphan",
        lazy="selectin", passive_deletes=True,
    )


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="healthy", nullable=False, index=True)
    #: Environment variable reference, e.g. ``${OPENAI_KEY_01}`` - never the key.
    secret_ref: Mapped[str | None] = mapped_column(String(128))
    secret_fingerprint: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: Live counters are mirrored here so restarts keep some history.
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    rate_limit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(48))
    cooldown_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    provider: Mapped[Provider] = relationship(back_populates="credentials")


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("model_id", "provider_id", "upstream_model", name="uq_deployment"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Provider native model name sent upstream.
    upstream_model: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    context_window: Mapped[int] = mapped_column(Integer, default=128_000, nullable=False)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_cost_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    output_cost_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    model: Mapped[Model] = relationship(back_populates="deployments")
    provider: Mapped[Provider] = relationship(back_populates="deployments")


class ModelAlias(Base):
    __tablename__ = "model_aliases"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    targets: Mapped[list[str]] = mapped_column(JSON, default=list)
    strategy: Mapped[str] = mapped_column(String(24), default="capability")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    weights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fallback_to_local: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Request telemetry
# --------------------------------------------------------------------------- #
class RequestRecord(Base):
    __tablename__ = "requests"
    __table_args__ = (Index("ix_requests_started_at", "started_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resolved_model: Mapped[str | None] = mapped_column(String(128))
    alias: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_id: Mapped[str | None] = mapped_column(String(64), index=True)
    deployment_id: Mapped[str | None] = mapped_column(String(128))
    credential_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(48), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    routing_reason: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list[RequestAttempt]] = relationship(
        back_populates="request", cascade="all, delete-orphan", lazy="selectin"
    )


class RequestAttempt(Base):
    __tablename__ = "request_attempts"
    __table_args__ = (Index("ix_attempts_request_id", "request_id", "attempt_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    deployment_id: Mapped[str | None] = mapped_column(String(128))
    credential_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="success")
    error_type: Mapped[str | None] = mapped_column(String(48), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text)

    request: Mapped[RequestRecord] = relationship(back_populates="attempts")


class HealthCheck(Base):
    __tablename__ = "health_checks"
    __table_args__ = (Index("ix_health_checked_at", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    credential_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: manual | startup | scheduled
    kind: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error_type: Mapped[str | None] = mapped_column(String(48))
    detail: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    credential_id: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: 输入里命中供应商侧前缀缓存的部分（OpenAI prompt_tokens_details /
    #: Anthropic cache_read_input_tokens）。2026-10-24 加入：不提这列，
    #: DeepSeek 的缓存折扣完全不可见，成本按全价高估近一个数量级。
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
# ZK-Agent: batch coding-task sessions (transcript = LLM replay history)
# --------------------------------------------------------------------------- #
class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    #: alias or model id the loop routes through (default ``zk-auto``)
    model: Mapped[str] = mapped_column(String(128), default="zk-auto")
    workspace: Mapped[str] = mapped_column(String(500), default="")
    #: running | waiting_approval | done | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AgentMessage(Base):
    """One transcript row. ``role``/``content``/``data`` double as the LLM
    replay history (user / assistant / tool rows); display-only rows carry
    ``kind`` values the loop skips when rebuilding the context."""

    __tablename__ = "agent_messages"
    __table_args__ = (Index("ix_agent_messages_session", "session_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: user | assistant | tool  (OpenAI replay roles)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: task | message | tool_call | tool_result | approval | summary | note
    kind: Mapped[str] = mapped_column(String(24), default="message")
    content: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
# 事件中台: 一次「信号 -> 规则 -> 任务」的完整留档
# --------------------------------------------------------------------------- #
class EventLaterRecord(Base):
    """「到点再核实」的复查（回执里的 ``later``）。

    为什么单独一张表而不是塞进 events（2026-11-04）：
    复查不是事件——它是**还没发生的一件事**。塞进 events 会污染队列统计、
    让「等认领」里混着「还没到点」的东西。到点才由采集器转成一条真正的事件。

    同一个 ``(event_id, key)`` 只该有一条 pending：Agent 复查后往后推，
    是把旧的改成 superseded 再开一条新的，而不是排队三条。
    """

    __tablename__ = "event_laters"
    __table_args__ = (Index("ix_event_laters_at", "state", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: 这次复查的固定名字（Agent 自己起，用于「取消/覆盖」而不是「新增」）
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    about: Mapped[str] = mapped_column(Text, default="")
    #: pending | fired | cancelled | superseded
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    fired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EventRecord(Base):
    """自动化中台的一条事件。

    设计要点（都踩过坑，别改回去）：

    * ``dedupe_key`` 是**语义键**（来源+类型+对象+状态指纹），不是消息 id。
      来源侧网络抖一下会重发、同事补一句话又是一条 —— 按消息 id 去重等于没去重。
    * ``state`` 是单一事实来源，别用「有没有 session_id」推断进度：
      未命中规则的事件没有会话，但它不是失败。
    * ``payload`` 留原始 JSON。规则写错了、Agent 结论可疑时，这是唯一能复盘的证据。
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_state_received", "state", "received_at"),
        Index("ix_events_dedupe", "dedupe_key", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 来源标识：zkai-gateway / server / github / manual ...
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 事件类型：health.quarantined / config.drift / git.behind ...
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 受影响对象，如 "sensenova-03"、"PR#128"
    object: Mapped[str] = mapped_column(String(200), default="")
    #: 一句话摘要，直接进审批卡片标题
    summary: Mapped[str] = mapped_column(String(500), default="")
    #: 跳回原处的链接（控制台/PR/日志）
    link: Mapped[str | None] = mapped_column(String(1000))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)

    #: received(待认领) | running | done | failed | dropped(无规则) | waiting_decision
    state: Mapped[str] = mapped_column(String(24), default="received", index=True)
    #: 命中的规则名；NULL 表示没有规则接手（已 dropped）
    rule: Mapped[str | None] = mapped_column(String(64))
    #: 去重分组（规则里的 group）。认领时要按组限并发，所以必须落到列上
    group_name: Mapped[str | None] = mapped_column(String(64), index=True)
    #: 该组允许同时跑几条。与 group_name 一起构成认领时的容量判断
    parallel: Mapped[int] = mapped_column(Integer, default=1)
    role: Mapped[str | None] = mapped_column(String(64))
    cwd: Mapped[str | None] = mapped_column(String(500))
    #: 展开后真正交给执行体的指令（ask + 事件上下文）
    task: Mapped[str | None] = mapped_column(Text)
    #: 关联 agent_sessions.id，点开能跳回当时的会话
    session_id: Mapped[str | None] = mapped_column(String(32))
    verdict: Mapped[str | None] = mapped_column(Text)
    #: 这条结论是否需要人拍板（决定要不要弹卡片）
    need_decision: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: 认领者（worker 实例名），排查「谁接了活没交」用
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 派生深度：这条事件是第几跳产生的。
    #:
    #: 为什么必须落列（2026-11-04，借自 agent-autoloop 的 TTL 思路）：
    #: 审批闭环接上以后，规则可以 ``types: [xxx.decided]`` 把事件派回执行体，
    #: 而执行体又能发新事件 —— A 叫醒 B、B 叫醒 A 就是静默烧钱。
    #: 深度**由中台计算，不来自事件体**（执行体自己改不了），
    #: 且经过人工拍板也不清零（用了 hop 字段的规则才会 +1，见 service.ingest）。
    hops: Mapped[int] = mapped_column(Integer, default=0)
    #: 父事件 id（派生它的那条）。排查链路用，也用于在卡片上显示来源。
    parent_id: Mapped[int | None] = mapped_column(Integer)
    #: 最早可以认领的时刻（NULL = 立即）。回执里的 ``later`` 靠它实现「约个时间再醒」：
    #: 事件照样入库，但认领要等到这个点之后 —— 等的时候不占进程、不花 Token。
    not_before: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


__all__ = [
    "AgentMessage",
    "AgentSession",
    "Base",
    "Credential",
    "Deployment",
    "EventRecord",
    "HealthCheck",
    "Model",
    "ModelAlias",
    "Provider",
    "RequestAttempt",
    "RequestRecord",
    "UsageRecord",
    "to_datetime",
    "utcnow",
]
