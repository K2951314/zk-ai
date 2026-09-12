"""ORM models.

Nine tables, exactly as specified:

``providers``, ``models``, ``credentials``, ``deployments``, ``model_aliases``,
``requests``, ``request_attempts``, ``health_checks``, ``usage_records``.

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
        back_populates="provider", cascade="all, delete-orphan", lazy="selectin"
    )
    deployments: Mapped[list[Deployment]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", lazy="selectin"
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
        back_populates="model", cascade="all, delete-orphan", lazy="selectin"
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
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


__all__ = [
    "Base",
    "Credential",
    "Deployment",
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
