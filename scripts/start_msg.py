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
        print('用法: start_msg.py <提示键|banner> [--mode ...] [--port P] [--host H]',
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
    entry = MESSAGES.get(key)
    if entry is None:
        print("未知的提示键: " + key, file=sys.stderr)
        return 2
    title, body, hint = entry
    print()
    print("  " + title)
    for line in body:
        print("          " + line)
    if hint:
        print("          下一步：" + hint)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
