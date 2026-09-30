"""scripts/start_msg.py + the .cmd launchers that must stay pure ASCII.

AGENTS.md has a hard rule: .cmd/.bat must be ASCII-only + CRLF, because Chinese
text makes cmd.exe misparse bytes. But startup failures are exactly when the
operator needs a readable message. These tests pin both halves of that deal:
the .cmd files do NOT carry the words, and the helper they call DOES.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

from scripts import start_msg

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_CMD_FILES = ("start_gateway.cmd", "start_burner.cmd")


def test_cmd_launchers_stay_pure_ascii() -> None:
    """Chinese must never land in a .cmd (AGENTS.md: cmd.exe byte-offset desync)."""
    for name in _CMD_FILES:
        raw = (_SCRIPTS / name).read_bytes()
        assert all(b < 128 for b in raw), f"{name} 含非 ASCII 字节"
        assert raw.count(b"\r\n") == raw.count(b"\n"), f"{name} 不是纯 CRLF"


def test_cmd_launchers_delegate_words_to_the_python_helper() -> None:
    """.cmd 只负责 goto，中文文案必须在 start_msg.py 里。

    这是同一份契约的另一半：如果哪天有人把英文提示直接 echo 回 .cmd，运营者
    又会看到英文——而这个项目面向中文使用者。
    """
    for name in _CMD_FILES:
        text = (_SCRIPTS / name).read_text(encoding="ascii")
        for lineno, line in enumerate(text.splitlines(), 1):
            body = line.strip()
            if not body.startswith("echo"):
                continue
            # 允许的：空行、以及那条不可中文化的安装命令
            if body == "echo." or "astral.sh/uv/install.ps1" in body:
                continue
            raise AssertionError(f"{name}:{lineno} 又是英文 echo：{body[:70]}")


def test_every_key_the_cmds_ask_for_actually_exists() -> None:
    """.cmd 里点的键，helper 必须真的有——否则启动失败时一个字都不出。

    这是最坏的失败模式：提示静默缺失，运营者只看到一个空窗口，比英文提示更糟。
    """
    asked: set[str] = set()
    for name in _CMD_FILES:
        text = (_SCRIPTS / name).read_text(encoding="ascii")
        asked |= set(re.findall(r"start_msg\.py\s+([a-z-]+)", text))
    assert asked, "两个 .cmd 都没有调用 start_msg.py，说明又回到英文 echo 了"
    missing = sorted(k for k in asked if k not in start_msg.MESSAGES and k != "banner")
    assert not missing, f"helper 缺这些键，启动失败时会静默无提示：{missing}"


def test_the_helper_prints_chinese_for_every_key() -> None:
    """每条提示都真的含中文，且不带英文句子。"""
    for key, (title, body, hint) in start_msg.MESSAGES.items():
        for piece in (title, hint, *body):
            if not piece:
                continue
            assert any("\u4e00" <= c <= "\u9fff" for c in piece), (
                f"{key} 的提示不是中文：{piece!r}")


def test_the_helpers_own_messages_are_chinese() -> None:
    """helper 自己的提示（用法 / 未知键）也必须是中文。

    这两条只在用错时出现，最容易漏：写脚本的人只测正常路径。
    """
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        start_msg.main([])
    assert '用法' in buf.getvalue(), buf.getvalue()
    assert 'MSG usage' not in buf.getvalue()

    buf2 = io.StringIO()
    with contextlib.redirect_stderr(buf2):
        start_msg.main(["no-such-key"])
    assert '未知的提示键' in buf2.getvalue(), buf2.getvalue()
    assert 'MSG unknown key' not in buf2.getvalue()


def test_burner_argparse_errors_are_chinese() -> None:
    """argparse 自己的报错也已中文化——传错参数是运营者最常撞上的。

    2026-09-30 修：之前只中文化了 --help，error() 分支仍吐英文
    （invalid float value），调参时看到的还是一半英文一半中文。
    """
    import contextlib
    import io

    from scripts import burn_sensenova

    cases = (
        (["--safety-margin", "abc"], '值不对'),
        (["--no-such-flag"], '无效的参数'),
        (["--model"], '缺少参数值'),
    )
    for argv, expect in cases:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                burn_sensenova.parse_args(argv)
            except SystemExit:
                pass
            else:
                raise AssertionError(str(argv) + '本该被拒绝')
        text = err.getvalue()
        assert expect in text, (argv, text[-200:])


def test_ask_mode_keeps_stdout_clean_for_for_f() -> None:
    """ask-* 的 stdout 只允许有答案那一行。

    这一步踩过：input() 的提示符默认走 stdout，for /f 抓回来就成了
    "path > (你填的路径)"，变量被污染，导入直接用错路径。
    同理，中文说明也必须走 stderr。
    """
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        stdin = io.StringIO("D:" + chr(92) + "temp" + chr(92) + "x.zip" + chr(10))
        with mock.patch("sys.stdin", stdin):
            start_msg.main(["ask-zip"])

    lines = [x for x in out.getvalue().splitlines() if x.strip()]
    assert lines == ["D:" + chr(92) + "temp" + chr(92) + "x.zip"], lines
    assert any("请输入" in x for x in err.getvalue().splitlines()), err.getvalue()


def test_the_cmd_files_use_the_verified_for_f_pattern() -> None:
    """import_machine.cmd 必须用 usebackq 反引号模式读回输入。

    非反引号的 for /f 把内容当文件名，实测报 cannot find the file；
    反引号模式才是执行命令。这个模式是拿 cmd 实测过的，不是猜的。
    """
    text = (_SCRIPTS / "import_machine.cmd").read_text(encoding="ascii")
    for key in ("ask-zip", "ask-overwrite"):
        marker = "start_msg.py " + key
        assert marker in text, f"import_machine.cmd 没有调用 {key}"
        line = next(ln for ln in text.splitlines() if marker in ln)
        assert "usebackq" in line and "`" in line, line

