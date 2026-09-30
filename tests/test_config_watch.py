"""配置文件监听：改完 YAML 不用重启、不用手动 reload。

要守住的行为（每一条都对应一次真实事故或一个真实疑问）：

* 编辑 `config/models.yaml` 之后，网关**自己**接上——不再需要人记得 reload；
* 写了一半的文件不会被加载（编辑器 truncate-then-write 的中间态）；
* YAML 写坏了**不会**让网关倒下：保留上一份配置，把原因说出来；
* 同一个改动只 reload 一次，不会每 2 秒重来一遍；
* 关掉开关就回到「必须手动 reload」的旧行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config_watch import ConfigWatcher, watch_paths
from app.models.request import ChatCompletionRequest


class FakeClock:
    """Deterministic clock: tests decide when ``settle`` has elapsed."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("models: []\n", encoding="utf-8")
    (tmp_path / "config" / "providers.yaml").write_text("providers: []\n", encoding="utf-8")
    # 模板与备份不该被监听：编辑它们不构成配置变更
    (tmp_path / "config" / "models.example.yaml").write_text("models: []\n", encoding="utf-8")
    return tmp_path / "config"


def _watcher(config_dir: Path, clock: FakeClock, calls: list[list[Path]], **kw) -> ConfigWatcher:
    async def handler(paths: list[Path]) -> None:
        calls.append(paths)

    watcher = ConfigWatcher(
        paths=[config_dir / "models.yaml", config_dir / "providers.yaml"],
        on_change=handler,
        interval=1.0,
        settle=0.4,
        clock=clock,
        **kw,
    )
    watcher.reseed()  # 等价于 start() 的基线播种，但不真的起任务
    return watcher


async def test_a_finished_edit_reloads_once(config_dir: Path) -> None:
    """改完 → 自动 reload，且只一次。"""
    clock = FakeClock()
    calls: list[list[Path]] = []
    watcher = _watcher(config_dir, clock, calls)

    _write(config_dir / "models.yaml", "models:\n  - id: new\n")
    clock.advance(1.0)
    changed = await watcher.poll_once()
    assert changed == [], "第一次只发现差异，还不能动手（要等 settle）"

    clock.advance(1.0)
    changed = await watcher.poll_once()
    assert [p.name for p in changed] == ["models.yaml"]
    assert len(calls) == 1

    # 同一个改动不应该再来一遍
    clock.advance(5.0)
    assert await watcher.poll_once() == []
    assert len(calls) == 1, "签名已刷新，不能反复 reload 同一个改动"


async def test_a_half_written_file_is_never_loaded(config_dir: Path) -> None:
    """编辑器 truncate-then-write 的中间态：内容在动，就不能加载。"""
    clock = FakeClock()
    calls: list[list[Path]] = []
    watcher = _watcher(config_dir, clock, calls)

    _write(config_dir / "models.yaml", "models:\n  - id: half")  # 半行
    clock.advance(1.0)
    assert await watcher.poll_once() == []

    _write(config_dir / "models.yaml", "models:\n  - id: complete\n")  # 写完
    clock.advance(1.0)
    assert await watcher.poll_once() == [], "签名刚变完，还在 settle 窗口内"

    clock.advance(1.0)
    changed = await watcher.poll_once()
    assert [p.name for p in changed] == ["models.yaml"]
    assert len(calls) == 1


async def test_a_broken_file_keeps_the_previous_config(config_dir: Path) -> None:
    """YAML 写坏不能把网关带走：保留旧配置、报出原因、下一个改动自动接上。"""
    clock = FakeClock()
    calls: list[list[Path]] = []
    boom = {"count": 0}

    async def handler(paths: list[Path]) -> None:
        boom["count"] += 1
        if boom["count"] == 1:
            raise ValueError("bad yaml: models: [")
        calls.append(paths)

    watcher = ConfigWatcher(
        paths=[config_dir / "models.yaml"],
        on_change=handler,
        interval=1.0,
        settle=0.4,
        clock=clock,
    )
    watcher.reseed()

    _write(config_dir / "models.yaml", "models: [\n")  # 语法错误
    clock.advance(1.0)
    await watcher.poll_once()
    clock.advance(1.0)
    assert await watcher.poll_once() == []
    assert watcher.status()["last_error"], "失败必须说出来，不能静默"

    # 修好之后再改一次：要能接上（说明监听没被打断）
    _write(config_dir / "models.yaml", "models: []\n# fixed\n")
    clock.advance(1.0)
    await watcher.poll_once()
    clock.advance(1.0)
    assert [p.name for p in await watcher.poll_once()] == ["models.yaml"]
    assert watcher.status()["reload_count"] == 1
    assert watcher.status()["last_error"] is None


async def test_reseed_drops_a_change_we_already_applied(config_dir: Path) -> None:
    """reload 之后必须刷新基线——否则每次轮询都看见同一个差异。"""
    clock = FakeClock()
    calls: list[list[Path]] = []
    watcher = _watcher(config_dir, clock, calls)

    _write(config_dir / "models.yaml", "models:\n  - id: a\n")
    clock.advance(1.0)
    await watcher.poll_once()
    clock.advance(1.0)
    assert len(await watcher.poll_once()) == 1
    assert len(calls) == 1

    clock.advance(10.0)
    await watcher.poll_once()
    assert len(calls) == 1


async def test_start_does_not_reload_just_because_files_exist(config_dir: Path) -> None:
    """启动时不能因为「文件在」就 reload 一次。"""
    clock = FakeClock()
    calls: list[list[Path]] = []
    watcher = ConfigWatcher(
        paths=[config_dir / "models.yaml"],
        on_change=lambda paths: _record(calls, paths),
        interval=1.0,
        settle=0.4,
        clock=clock,
    )
    watcher.reseed()

    clock.advance(10.0)
    assert await watcher.poll_once() == []
    assert calls == []


async def _record(calls: list[list[Path]], paths: list[Path]) -> None:
    calls.append(paths)


def test_watch_paths_only_covers_files_that_were_loaded() -> None:
    """模板和 .bak 不是配置：编辑它们不该触发 reload。"""
    source_files = {"config": "config.yaml", "providers": "providers.yaml", "models": "models.yaml"}

    paths = watch_paths(Path("config"), source_files)

    # 顺序 = loader 实际读取的顺序；also 说明 .example.yaml / .bak-* 不在其中
    assert [p.name for p in paths] == ["config.yaml", "providers.yaml", "models.yaml"]


def test_watch_paths_survives_a_missing_file(tmp_path: Path) -> None:
    """漏一个文件不该让整个监听起不来（config.yaml 可以是单文件部署）。"""
    (tmp_path / "providers.yaml").write_text("providers: []\n", encoding="utf-8")

    paths = watch_paths(tmp_path, {"providers": "providers.yaml", "config": "config.yaml"})

    assert [p.name for p in paths] == ["providers.yaml"]


# --------------------------------------------------------------------------- #
# end to end: file edited -> watcher -> container.reload_config() -> router
# --------------------------------------------------------------------------- #
_MODELS_A = """\
models:
  - id: m1
    enabled: true
    context_window: 128000
    deployments:
      - id: m1-p1
        provider_id: p1
        model: m1
        priority: 100
        context_window: 128000
aliases:
  - name: zk-one
    targets: [m1]
"""

_MODELS_B = """\
models:
  - id: m2
    enabled: true
    context_window: 128000
    deployments:
      - id: m2-p1
        provider_id: p1
        model: m2
        priority: 100
        context_window: 128000
aliases:
  - name: zk-two
    targets: [m2]
"""

_PROVIDERS = """\
providers:
  - id: p1
    type: openai
    base_url: http://127.0.0.1:9/v1
    enabled: true
    credentials:
      - id: p1-01
        env: SOMETHING
        value: dev-only
"""


async def _container_in(tmp_path: Path):
    """A real container that loads YAML from *tmp_path* (no real DB, no network)."""
    from app.core.config import Settings
    from app.core.container import build_container
    from app.database.db import Database

    (tmp_path / "models.yaml").write_text(_MODELS_A, encoding="utf-8")
    (tmp_path / "providers.yaml").write_text(_PROVIDERS, encoding="utf-8")
    settings = Settings(environment="test", config_dir=tmp_path, data_dir=tmp_path)
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    return await build_container(
        settings, database=database, start_services=False
    )


async def test_editing_models_yaml_reaches_the_router_without_a_restart(tmp_path: Path) -> None:
    """这正是第 4 项要修的东西：改完 YAML，网关自己接上。

    断言「旧别名消失」而不是只断言「新别名出现」——否则一个把两份配置合并起来的
    假实现也能让测试变绿。
    """
    container = await _container_in(tmp_path)
    try:
        assert container.router.aliases.names() == ["zk-one"]
        container.start_config_watch()
        watcher = container.config_watcher
        assert watcher is not None, "默认应该开着"

        clock = FakeClock()
        watcher.clock = clock  # 让 settle 窗口可预测，不用真睡
        (tmp_path / "models.yaml").write_text(_MODELS_B, encoding="utf-8")

        clock.advance(1.0)
        await watcher.poll_once()
        clock.advance(1.0)
        fired = await watcher.poll_once()

        assert [p.name for p in fired] == ["models.yaml"]
        assert container.router.aliases.names() == ["zk-two"], "旧别名必须消失"
        assert container.config.aliases["zk-two"].targets == ["m2"]
        # 路由真的按新配置走，不是只换了字段
        decision = container.router.plan(
            ChatCompletionRequest(
                model="zk-two", messages=[{"role": "user", "content": "hi"}]
            )
        )
        assert [c.model.id for c in decision.candidates if c.eligible] == ["m2"]
    finally:
        await container.shutdown()


async def test_the_watcher_can_be_switched_off(tmp_path: Path) -> None:
    """关掉就回到旧行为：文件变了也不动（运营者要「我说了才动」时有这个选择）。"""
    from app.core.config import Settings
    from app.core.container import build_container
    from app.database.db import Database

    (tmp_path / "models.yaml").write_text(_MODELS_A, encoding="utf-8")
    (tmp_path / "providers.yaml").write_text(_PROVIDERS, encoding="utf-8")
    settings = Settings(
        environment="test", config_dir=tmp_path, data_dir=tmp_path, watch_config=False
    )
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    container = await build_container(settings, database=database, start_services=False)
    try:
        container.start_config_watch()
        assert container.config_watcher is None, "关了就不该有监听器"
        (tmp_path / "models.yaml").write_text(_MODELS_B, encoding="utf-8")
        assert container.router.aliases.names() == ["zk-one"], "没监听就不能自己变"
    finally:
        await container.shutdown()


async def test_a_broken_yaml_does_not_take_the_gateway_down(tmp_path: Path) -> None:
    """写坏 YAML：保留上一份配置并报错，网关继续服务。"""
    container = await _container_in(tmp_path)
    try:
        container.start_config_watch()
        watcher = container.config_watcher
        clock = FakeClock()
        watcher.clock = clock

        (tmp_path / "models.yaml").write_text("models: [\n  - 坏\n", encoding="utf-8")
        clock.advance(1.0)
        await watcher.poll_once()
        clock.advance(1.0)
        assert await watcher.poll_once() == [], "加载失败不该报告成「已 reload」"

        assert container.router.aliases.names() == ["zk-one"], "旧配置必须还在"
        assert watcher.status()["last_error"], "为什么没生效必须说得出来"
    finally:
        await container.shutdown()
