'''网关正在启动（托盘方式，不弹窗口）。

.cmd 调用本脚本，因为它自己必须保持纯 ASCII
(AGENTS.md：中文会让 cmd.exe 字节错位误解析，历史上生成过乱码文件、跳过行。)

但启动失败又是运营者最容易卡住的时刻，那些提示必须是中文。'''

from __future__ import annotations

import sys

MESSAGES = {
    "tray-failed": (
        '【错误】托盘进程没起来，所以既没有图标、网关也没跑。',
        ['它自己的输出：data\\tray_gateway.log',
         '网关日志: data\\gateway.log'],
        '日志里若是 ModuleNotFoundError，说明 .venv 没跟上依赖 → 跑一次：uv sync',
    ),
    "tray-failed-burner": (
        '【错误】托盘进程没起来，所以既没有图标、网关也没跑。',
        ['它自己的输出：data\\tray_burner.log',
         '消耗器日志: data\\burn_sensenova.log'],
        '日志里若是 ModuleNotFoundError，说明 .venv 没跟上依赖 → 跑一次：uv sync',
    ),
    "no-listen": (
        '【错误】托盘起来了，但端口一直没有响应。',
        ['服务端日志: data\\gateway.log',
         '也可以右键托盘图标 → 查看日志'],
        '重跑一次本脚本; 反复失败请把 data/gateway.log 末尾几行发出来',
    ),
    "cancelled": ('已取消启动。', [], ''),
    "env-fill": ('按上面提示填好 .env，再跑一次本脚本。', [], ''),
    "venv-broken": (
        '【提示】Python 环境 .venv 不存在或已损坏',
        [], ''),
    "rebuilding": ('正在用 uv sync 重建环境，请稍等', [], ''),
    "rebuilding-burner": ('正在用 uv sync 重建环境', [], ''),
    "sync-failed": (
        '【错误】uv sync 失败，环境没搭成。',
        [], '看上面几行；常见原因是网络或代理'),
    "venv-rebuilt": ('.venv 已重建完成。', [], ''),
    "uv-missing": (
        '【错误】没装 uv，没法准备Python 环境。',
        [], '然后关掉这个窗口、重新打开（让 PATH 生效）再启动'),
    "venv-broken-burner": (
        '【提示】Python 环境 .venv 不存在或已损坏',
        [], ''),
    "uv-reopen": (
        '然后关掉这个窗口、重新打开（让 PATH 生效）再启动',
        [], ''),
    "venv-missing-cmd": (
        '【错误】没找到 .venv。',
        ['先双击 scripts\\start_gateway.cmd 跑一遍，', '它会自动把环境重建好。'],
        '',
    ),
    "export-banner": (
        '正在导出本机的 ZK-AI 身份（Key + 配置 + 数据库）。',
        ['稍后会要求输入迁移密码，记住它，新电脑要用同一个。', '输入时不回显（安全起见）。'],
        '',
    ),
    "failed-see-above": (
        '失败了，原因看上面几行。',
        [],
        '',
    ),
    "drag-zip": (
        '把迁移包的 .zip 拖进这个窗口，然后回车。',
        [],
        '',
    ),
    "no-archive": (
        '【错误】没给迁移包。',
        [],
        '',
    ),
    "overwrite-guard": (
        '这些文件已经存在，这是覆盖保护在工作，不算失败。',
        [],
        '',
    ),
    "ask-overwrite": (
        '要用 --overwrite 重试吗？',
        [],
        '覆盖前会先把当前文件备份到 imports_backup\\',
    ),
    "prompt-zip": (
        '请输入迁移包的 .zip 路径',
        [],
        '',
    ),
    "prompt-overwrite": (
        '要用 --overwrite 重试吗？ y/N',
        [],
        '覆盖前会先把当前文件备份到 imports_backup',
    ),
}

BANNER = {
    "gateway": [
        'ZK-AI 网关正在启动（托盘方式，不弹窗口）。',
        '  端口：{port}   优先级：命令行参数 > .env 的 ZKAI_PORT > 默认 8317',
        '  地址：{host}   0.0.0.0 表示局域网内其他机器也能访问',
        '  托盘：屏幕右下角，绿色=运行中 / 蓝色=已停止或卡住',
        '  停止：右键托盘图标 → 退出',
        '  界面：浏览器会自动打开，令牌已从 .env 带好，不用手填',
    ],
    "burner": [
        '商汤积分消耗器正在启动（托盘方式，不弹窗口）。',
        '  托盘：屏幕右下角，绿色=运行中 / 蓝色=已停止或卡住',
        '  日志：data\\burn_sensenova.log',
        '  停止：右键托盘图标 → 退出',
    ],
}


def main(argv: list[str]) -> int:
    if not argv:
        print('用法: start_msg.py <提示键|banner|ask>',
              file=sys.stderr)
        print('      ask-zip → 打印中文提示并读取路径',
              file=sys.stderr)
        print('      ask-overwrite → 打印中文提示并读取 y/N',
              file=sys.stderr)
        return 2
    key = argv[0]
    opts = {k: (argv[argv.index(k) + 1] if k in argv else "")
            for k in ("--mode", "--port", "--host")}
    if key == "banner":
        lines = BANNER.get(opts["--mode"] or "gateway", BANNER["gateway"])
        print()
        for line in lines:
            print("  " + line.format(port=opts["--port"], host=opts["--host"]))
        print()
        return 0
    if key.startswith("ask-"):
        return _ask(key[len("ask-"):], argv)
    entry = MESSAGES.get(key)
    if entry is None:
        print("未知的提示键: " + key, file=sys.stderr)
        return 2
    _show(entry)
    return 0


def _show(entry: tuple) -> None:
    """渲染一条提示：标题 + 正文 + 下一步。 main() 与 _ask() 共用。"""
    title, body, hint = entry
    print()
    print("  " + title)
    for line in body:
        print("          " + line)
    if hint:
        print("          " + '下一步：' + hint)
    print()


def _prompt_to_stderr(entry: tuple) -> None:
    """Like _show() but writes to stderr: stdout is reserved for the
    single line `for /f` reads back, so nothing else may land there."""
    _title, body, hint = entry
    print(file=sys.stderr)
    print('  ' + _title, file=sys.stderr)
    for _line in body:
        print('          ' + _line, file=sys.stderr)
    if hint:
        print('          ' + hint, file=sys.stderr)


def _ask(what: str, argv: list[str]) -> int:
    """打印中文提示并读一行键盘输入，把结果写到 stdout。

    为什么不让 .cmd 用 set /p：AGENTS.md 要求 .cmd 纯 ASCII，而 set /p 的
    提示串是给人看的（archive path / Retry with --overwrite）。改成这里收
    键盘，.cmd 里就一个英文字母都不剩了。.cmd 用 for /f 拿回去。
    """
    if what == "zip":
        _prompt_to_stderr(MESSAGES["prompt-zip"])
        prompt = '路径 > '
    elif what == "overwrite":
        _prompt_to_stderr(MESSAGES["prompt-overwrite"])
        prompt = ' [y/N] > '
    else:
        print('未知的读取项: ' + what, file=sys.stderr)
        return 2
    try:
        # The prompt MUST go to stderr: `for /f` captures stdout, and a prompt
        # mixed into stdout would make the batch variable hold
        # 'path > D:\temp\x.zip' instead of just the path.
        print(prompt, end="", file=sys.stderr)
        sys.stderr.flush()
        answer = input("").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    print(answer)   # .cmd 用 for /f 读回这一行
    return 0



if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
