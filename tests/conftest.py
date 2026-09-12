"""Shared test fixtures: a fully wired gateway backed by programmable fake providers.

No test touches the network: every provider is a :class:`FakeAdapter` whose
behaviour (text, HTTP status, exception, delay) is scripted per call. Retry delays
are zeroed through an injected sleeper so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.config import AppConfig, Settings
from app.core.container import Container, build_container
from app.credentials.pool import CredentialPool
from app.database.db import Database
from app.models.credential import CredentialRuntime
from app.models.provider import (
    AliasStrategy,
    CapabilityScores,
    CredentialConfig,
    DeploymentConfig,
    ModelAliasConfig,
    ModelConfig,
    ProviderConfig,
    ProviderType,
)
from app.models.request import ChatCompletionRequest
from app.models.response import ChatCompletionChunk, ChatCompletionResponse, Usage
from app.providers.base import HealthCheckResult, ProviderAdapter, ProviderContext
from app.retry.policy import RetryPolicy
from app.routing.router import Router

# --------------------------------------------------------------------------- #
# Behaviour scripting
# --------------------------------------------------------------------------- #


@dataclass
class Behavior:
    """One scripted upstream response."""

    text: str = "fake reply"
    status: int | None = None
    error: BaseException | None = None
    chunks: int = 3
    delay: float = 0.0
    prompt_tokens: int = 10
    completion_tokens: int = 5
    model_list: list[str] = field(default_factory=list)


class FakeAdapter(ProviderAdapter):
    """In-memory provider that records every call."""

    supported_params = frozenset({"model", "messages", "temperature", "max_tokens", "stream"})

    def __init__(self, config: ProviderConfig, *, default: Behavior | None = None) -> None:
        super().__init__(config)
        self.behaviors: deque[Behavior] = deque()
        self.default = default or Behavior()
        self.calls: list[dict[str, Any]] = []
        self.streams_opened = 0
        self.streams_closed = 0
        self.streams_completed = 0
        self.health_calls = 0
        self.fail_health = False
        self.unsupported_seen: list[str] = []

    # ------------------------------------------------------------------ #
    def queue(self, *behaviors: Behavior) -> FakeAdapter:
        self.behaviors.extend(behaviors)
        return self

    def queue_status(self, *statuses: int) -> FakeAdapter:
        return self.queue(*(Behavior(status=status) for status in statuses))

    def _next(self) -> Behavior:
        return self.behaviors.popleft() if self.behaviors else self.default

    def _record(self, ctx: ProviderContext, *, stream: bool) -> Behavior:
        behavior = self._next()
        self.calls.append(
            {
                "provider": self.provider_id,
                "credential_id": ctx.credential_id,
                "model": ctx.upstream_model,
                "attempt": ctx.attempt,
                "stream": stream,
                "status": behavior.status,
                "error": type(behavior.error).__name__ if behavior.error else None,
            }
        )
        return behavior

    def _error_for(self, status: int) -> Exception:
        info = self.classifier.classify_status(status, body={"error": {"message": "fake"}})
        return info.to_error(provider=self.provider_id, model="fake")

    # ------------------------------------------------------------------ #
    # Abstract implementation
    # ------------------------------------------------------------------ #
    def _auth_headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        return {"authorization": f"Bearer {credential.secret}"} if credential and credential.secret else {}

    def build_payload(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig
    ) -> tuple[dict[str, Any], list[str]]:
        payload = {
            "model": deployment.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
        }
        unsupported = [key for key in request.extra_params() if key not in self.supported_params]
        if request.tools:
            unsupported.append("tools")
        self.unsupported_seen.extend(unsupported)
        return payload, sorted(set(unsupported))

    async def chat(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> ChatCompletionResponse:
        behavior = self._record(ctx, stream=False)
        # Real adapters always build a payload first; do the same so that
        # unmappable parameters are recorded here too.
        self.build_payload(request, ctx.deployment)
        if behavior.delay:
            await asyncio.sleep(behavior.delay)
        if behavior.error is not None:
            raise behavior.error
        if behavior.status is not None and behavior.status >= 400:
            raise self._error_for(behavior.status)
        return ChatCompletionResponse.simple(
            model=ctx.upstream_model,
            content=behavior.text,
            usage=Usage.build(behavior.prompt_tokens, behavior.completion_tokens),
            provider_id=self.provider_id,
        )

    async def stream(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> AsyncIterator[ChatCompletionChunk]:
        behavior = self._record(ctx, stream=True)
        self.build_payload(request, ctx.deployment)
        self.streams_opened += 1
        completed = False
        if behavior.error is not None:
            self.streams_closed += 1
            raise behavior.error
        if behavior.status is not None and behavior.status >= 400:
            self.streams_closed += 1
            raise self._error_for(behavior.status)
        try:
            yield self._chunk(model=ctx.upstream_model, role="assistant")
            for index in range(behavior.chunks):
                if behavior.delay:
                    await asyncio.sleep(behavior.delay)
                yield self._chunk(model=ctx.upstream_model, content=f"part{index} ")
            completed = True
            self.streams_completed += 1
            yield self._chunk(
                model=ctx.upstream_model,
                finish_reason="stop",
                usage=Usage.build(behavior.prompt_tokens, behavior.completion_tokens),
            )
        finally:
            self.streams_closed += 1
            if not completed and self.streams_completed < self.streams_opened:
                # aclose() landed here: the upstream connection was released early.
                pass

    async def list_models(self, credential: CredentialRuntime | None = None) -> list[str]:
        return ["fake-model"]

    async def health_check(self, credential: CredentialRuntime | None = None) -> HealthCheckResult:
        self.health_calls += 1
        if self.fail_health:
            return HealthCheckResult(
                provider_id=self.provider_id,
                credential_id=credential.id if credential else None,
                ok=False,
                error_type="authentication_error",
                detail="fake unhealthy",
            )
        return HealthCheckResult(
            provider_id=self.provider_id,
            credential_id=credential.id if credential else None,
            ok=True,
            latency_ms=1.25,
            models=["fake-model"],
        )

    def normalize_response(self, payload: dict[str, Any]) -> ChatCompletionResponse:
        return ChatCompletionResponse.simple(model="fake", content=str(payload))


# --------------------------------------------------------------------------- #
# Config / harness builders
# --------------------------------------------------------------------------- #


def make_provider(
    provider_id: str = "fake",
    *,
    key_ids: tuple[str, ...] = ("key-1", "key-2"),
    provider_type: ProviderType = ProviderType.OPENAI,
    enabled: bool = True,
    keyless: bool = False,
) -> ProviderConfig:
    credentials: list[CredentialConfig] = []
    if not keyless:
        for index, key_id in enumerate(key_ids):
            credentials.append(
                CredentialConfig(
                    id=key_id,
                    env=f"${key_id.upper().replace('-', '_')}",
                    priority=100 - index * 10,
                    value=f"secret-{key_id}",  # inline allowed in tests
                )
            )
    return ProviderConfig(
        id=provider_id,
        type=provider_type,
        base_url="http://127.0.0.1:9/v1",
        enabled=enabled,
        credentials=credentials,
    )


def make_model(
    model_id: str,
    *,
    provider_id: str = "fake",
    upstream: str | None = None,
    priority: int = 100,
    capabilities: dict[str, float] | None = None,
    context_window: int = 128_000,
    enabled: bool = True,
) -> ModelConfig:
    scores = {
        "coding": 5.0,
        "reasoning": 5.0,
        "tool_use": 5.0,
        "vision": 0.0,
        "long_context": 5.0,
        "structured_output": 5.0,
        "speed": 5.0,
        "cost": 5.0,
    }
    scores.update(capabilities or {})
    return ModelConfig(
        id=model_id,
        enabled=enabled,
        context_window=context_window,
        capabilities=CapabilityScores(**scores),
        deployments=[
            DeploymentConfig(
                id=f"{model_id}-dep",
                provider_id=provider_id,
                model=upstream or model_id,
                priority=priority,
                context_window=context_window,
            )
        ],
    )


def make_alias(name: str, targets: list[str], *, strategy: AliasStrategy = AliasStrategy.CAPABILITY,
               weights: dict[str, float] | None = None,
               requires: dict[str, float] | None = None) -> ModelAliasConfig:
    return ModelAliasConfig(
        name=name,
        targets=targets,
        strategy=strategy,
        weights=weights or {},
        requires=requires or {},
    )


def make_config(
    *,
    providers: list[ProviderConfig] | None = None,
    models: list[ModelConfig] | None = None,
    aliases: list[ModelAliasConfig] | None = None,
    policy: RetryPolicy | None = None,
    admin_token: str | None = None,
) -> AppConfig:
    settings = Settings(
        environment="test",
        admin_token=admin_token,
        health_check_mode="off",
        health_check_on_startup=False,
        allow_inline_secrets=True,
        config_dir="config",
    )
    return AppConfig(
        settings=settings,
        providers={p.id: p for p in (providers or [make_provider()])},
        models={m.id: m for m in (models or [make_model("fake-model")])},
        aliases={a.name: a for a in (aliases or [])},
        retry=policy or RetryPolicy(
            max_retries_per_credential=2,
            max_credentials_per_deployment=3,
            max_deployments=3,
            max_total_attempts=8,
            base_delay=0.0,
            max_delay=0.0,
            jitter=0.0,
            jitter_mode="none",
        ),
    )


class Harness:
    """Wired gateway with fake providers."""

    def __init__(self, container: Container, adapters: dict[str, FakeAdapter]) -> None:
        self.container = container
        self.adapters = adapters
        self.sleeps: list[float] = []

    # Convenience accessors -------------------------------------------- #
    @property
    def pool(self) -> CredentialPool:
        return self.container.pool

    @property
    def router(self) -> Router:
        return self.container.router

    @property
    def scheduler(self):
        return self.container.scheduler

    @property
    def service(self):
        return self.container.request_service

    @property
    def adapter(self) -> FakeAdapter:
        return next(iter(self.adapters.values()))

    def all_calls(self) -> list[dict[str, Any]]:
        return [call for adapter in self.adapters.values() for call in adapter.calls]

    async def request(
        self, model: str = "fake-model", *, stream: bool = False, **overrides: Any
    ) -> Any:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        }
        payload.update(overrides)
        request = ChatCompletionRequest(**payload)
        if stream:
            return [event async for event in self.service.stream(request, request_id="test-req")]
        return await self.service.chat(request, request_id="test-req")

    async def collect_stream(
        self, model: str = "fake-model", **overrides: Any
    ) -> list[Any]:
        events = await self.request(model, stream=True, **overrides)
        return list(events)


async def build_harness(
    config: AppConfig,
    *,
    adapters: dict[str, FakeAdapter] | None = None,
    in_memory: bool = True,
    start_services: bool = False,
) -> Harness:
    """Wire a container using fake adapters and an in-memory database."""
    pool = CredentialPool(allow_inline_secrets=True)
    for provider in config.providers.values():
        pool.register_provider(provider)

    router = Router(config)
    fakes = adapters or {}
    for provider_id, adapter in fakes.items():
        router.register_adapter(provider_id, adapter)

    url = "sqlite+aiosqlite:///:memory:" if in_memory else "sqlite+aiosqlite:///./data/test.db"
    database = Database(url)
    await database.init()

    container = await build_container(
        config.settings,
        config=config,
        database=database,
        pool=pool,
        router=router,
        policy=config.retry,
        start_services=start_services,
    )
    # Inject a zero-cost sleeper so retry tests never actually wait.
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    container.scheduler._sleep = fake_sleep

    harness = Harness(container, fakes)
    harness.sleeps = recorded
    return harness


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def provider_config() -> ProviderConfig:
    return make_provider("fake", key_ids=("key-1", "key-2", "key-3"))


@pytest.fixture
def fake_adapter(provider_config: ProviderConfig) -> FakeAdapter:
    return FakeAdapter(provider_config)


@pytest.fixture
async def harness(provider_config: ProviderConfig, fake_adapter: FakeAdapter) -> AsyncIterator[Harness]:
    """Two-model, three-key gateway with a single fake provider."""
    config = make_config(
        providers=[provider_config],
        models=[
            make_model("fake-model", capabilities={"coding": 6.0, "vision": 0.0}),
            make_model("fake-smart", priority=90, capabilities={"coding": 9.0, "reasoning": 9.0}),
            make_model("fake-cheap", priority=80, capabilities={"cost": 10.0, "speed": 9.0}),
        ],
        aliases=[
            make_alias("zk-test", ["fake-model", "fake-smart", "fake-cheap"]),
            make_alias("zk-smart", ["fake-smart"], strategy=AliasStrategy.PRIORITY),
        ],
    )
    h = await build_harness(config, adapters={"fake": fake_adapter})
    yield h
    await h.container.shutdown()


@pytest.fixture
def requests_factory():
    """Build a ChatCompletionRequest quickly."""

    def factory(model: str = "fake-model", **kwargs: Any) -> ChatCompletionRequest:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
        }
        payload.update(kwargs)
        return ChatCompletionRequest(**payload)

    return factory


def now() -> float:
    return time.time()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _fast_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep test output readable."""
    import logging

    logging.getLogger("zkai").setLevel(logging.CRITICAL)
    yield
