"""消耗器日志通道：增量游标、文件缺失、半行容忍、降级 tail。

半行是这里唯一的真坑：消耗器 append 写日志，控制台按字节偏移读，读到一行写
到一半的内容概率不为 0。若不等剩余字节到齐就推给前端，一条日志会被拆成两条
——运营者看到的汇总就是残缺的，而这正是他最需要读的那行。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.api import burner_log


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text(
        "".join(f"{time.strftime('%Y-%m-%d')} {line}\n" for line in lines),
        encoding="utf-8",
    )


def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """把轮询间隔调到 10ms：用例不再真实等秒级，半行这种中间态也能稳定捕获。"""
    monkeypatch.setattr(burner_log, "_POLL_SECONDS", 0.01)


class TestReadLines:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert burner_log._read_lines(tmp_path / "nope.log", within_seconds=60) == []

    def test_reads_recent_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "burn_sensenova.log"
        _write_log(log, ["00:00:01 INFO 第一条", "00:00:02 INFO 第二条"])
        lines = burner_log._read_lines(log, within_seconds=86400)
        assert len(lines) == 2

    def test_within_seconds_zero_means_everything(self, tmp_path: Path) -> None:
        log = tmp_path / "burn_sensenova.log"
        _write_log(log, ["00:00:01 INFO 老日志"])
        assert len(burner_log._read_lines(log, within_seconds=0)) == 1

    def test_unparseable_lines_are_kept(self, tmp_path: Path) -> None:
        """时间解析不出来的行要保留——异常日志往往正是要找的那条。"""
        log = tmp_path / "burn_sensenova.log"
        log.write_text(
            "Traceback (most recent call last):\n  File x\n", encoding="utf-8"
        )
        assert len(burner_log._read_lines(log, within_seconds=60)) == 2

    def test_leading_epoch(self) -> None:
        assert burner_log._leading_epoch("2026-10-03 18:48:29 INFO x") is not None
        assert burner_log._leading_epoch("not a timestamp") is None
        assert burner_log._leading_epoch("") is None


    def test_reads_only_the_tail_of_a_huge_log(self, tmp_path: Path) -> None:
        """日志长到几百 MB 时不能整份读。

        实测本地 burn_sensenova.log 105MB / 63.7 万行，从头读这个端点要 12.7 秒
        ——而它挂在一个每 10 秒自动刷新的页面上。所以只读尾部固定字节数。
        """
        log = tmp_path / "burn_sensenova.log"
        stamp = time.strftime("%Y-%m-%d")
        # 写一个远超 _TAIL_BYTES 的文件：前面全是噪声，只有尾部该被看到
        filler = "x" * 4096 + "\n"
        with log.open("w", encoding="utf-8") as fh:
            for i in range(200):  # ~800KB > 256KB
                fh.write(f"{stamp} 00:00:01 INFO noise-{i:04d} " + filler)
            fh.write(f"{stamp} 12:34:56 INFO 最后一条\n")
        assert log.stat().st_size > burner_log._TAIL_BYTES

        lines = burner_log._read_lines(log, within_seconds=0)
        assert lines, "尾部的行必须能读到"
        assert any("最后一条" in line for line in lines)
        # 前面的噪声不该整份进来（只可能残留被截断边界附近的那一小段）
        assert len(lines) < 300
        assert not any("noise-0000 " in line for line in lines)


class TestFollow:
    @pytest.mark.asyncio
    async def test_half_line_is_not_split_into_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """预热之后追加的半行要等换行到齐才推。

        预热底料发完后游标停在 EOF，所以半行只会出现在「追加到一半」的那一刻。
        读到半行就推 = 把一条汇总拆成两条残缺的推给运营者，而汇总恰恰是他
        最需要读的那行。
        """
        log = tmp_path / "burn_sensenova.log"
        log.write_text("", encoding="utf-8")
        _fast_poll(monkeypatch)
        gen = burner_log._follow(log, lines=10, within_seconds=0)
        assert "已连接" in await gen.__anext__()  # 预热底料 + 游标同一次算出
        await asyncio.sleep(0.05)  # 空转几轮，等下一次轮询

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} INFO 汇总：烧了 12")
        await asyncio.sleep(0.05)  # 一轮轮询：读到半行，应停在 carry 里

        with log.open("a", encoding="utf-8") as fh:
            fh.write("34 积分\n")
        out = await asyncio.wait_for(gen.__anext__(), timeout=5)
        assert "汇总：烧了 1234 积分" in out
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_truncation_restarts_from_beginning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """copytruncate 之后偏移必须重置，否则流永远卡在「读到空」。

        不重置的表现很隐蔽：文件变小、`size < offset`，若跳过重置就会一直
        空转下去——页面显示「实时跟随中」但一行都不再来。
        """
        log = tmp_path / "burn_sensenova.log"
        # 用 ASCII：截断点可能落在一个多字节字符中间，那属于 logrotate 的
        # copytruncate 与原子写入竞争，不是这里要守的行为。
        _write_log(log, ["00:00:01 INFO first line"])
        _fast_poll(monkeypatch)
        gen = burner_log._follow(log, lines=10, within_seconds=0)
        warm = await gen.__anext__()  # 历史 + 控制事件 + 游标，同一个 chunk
        assert "first line" in warm and "已连接" in warm
        await asyncio.sleep(0.05)

        _write_log(log, ["00:00:02 INFO after truncate"])
        # 截断后游标重置，下一轮就会读到新文件的内容。写文件和读是并发的，
        # 断言「有东西来了」而不是逐字节对齐——后者会把测试变成赛态。
        out = await asyncio.wait_for(gen.__anext__(), timeout=5)
        assert out.startswith("data: "), repr(out)
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_missing_file_does_not_crash_the_stream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """文件不存在：流不断，等它出现再推。

        真实场景是「消耗器还没启动」或「刚 copytruncate 完正在重建」——这两种
        情况下流不该断，运营者打开页面就能看到日志开始滚。
        """
        log = tmp_path / "gone.log"
        _fast_poll(monkeypatch)
        gen = burner_log._follow(log, lines=10, within_seconds=0)
        assert "已连接" in await gen.__anext__()
        await asyncio.sleep(0.05)  # 一轮空转：文件不存在也不抛异常

        _write_log(log, ["00:00:09 INFO 迟到的第一行"])
        out = await asyncio.wait_for(gen.__anext__(), timeout=5)
        assert "迟到的第一行" in out
        await gen.aclose()


class TestLogPath:
    def test_log_lives_next_to_the_ledger(self) -> None:
        """与账本同目录：zkai-burner.service 的 --log-file 指的就是这里。"""
        assert burner_log._LOG_NAME == "burn_sensenova.log"


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_tail_returns_plain_text(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from tests.conftest import build_harness, make_alias, make_config, make_model

        _write_log(
            tmp_path / "burn_sensenova.log",
            ["00:00:01 INFO 第一次", "00:00:02 INFO 第二次"],
        )
        config = make_config(
            models=[make_model("fake-model")],
            aliases=[make_alias("zk-auto", ["fake-model"])],
            admin_token="t",
        )
        config.settings.data_dir = tmp_path
        harness = await build_harness(config)
        app = create_app(config.settings)
        app.state.container = harness.container
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://zkai.test"
            ) as client:
                resp = await client.get(
                    "/admin/burner/log/tail",
                    headers={"X-Admin-Token": "t"},
                )
            assert resp.status_code == 200
            body = resp.text
            assert "第一次" in body and "第二次" in body
        finally:
            await harness.container.shutdown()

    @pytest.mark.asyncio
    async def test_tail_explains_a_missing_log(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from tests.conftest import build_harness, make_alias, make_config, make_model

        config = make_config(
            models=[make_model("fake-model")],
            aliases=[make_alias("zk-auto", ["fake-model"])],
            admin_token="t",
        )
        config.settings.data_dir = tmp_path
        harness = await build_harness(config)
        app = create_app(config.settings)
        app.state.container = harness.container
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://zkai.test"
            ) as client:
                resp = await client.get(
                    "/admin/burner/log/tail",
                    headers={"X-Admin-Token": "t"},
                )
            assert resp.status_code == 200
            assert "还没有日志" in resp.text
        finally:
            await harness.container.shutdown()

    def test_routes_are_registered(self) -> None:
        paths = {r.path for r in burner_log.router.routes}
        assert "/admin/burner/log/tail" in paths
        assert "/admin/burner/log/stream" in paths

    def test_stream_disables_proxy_buffering(self) -> None:
        """X-Accel-Buffering 必须在：Caddy/nginx 默认会把 SSE 缓冲成一次性返回。"""
        source = Path(burner_log.__file__).read_text(encoding="utf-8")
        assert 'X-Accel-Buffering' in source
        assert "Cache-Control" in source
