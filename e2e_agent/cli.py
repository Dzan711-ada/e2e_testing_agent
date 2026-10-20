"""One URL-based Agent entry; fixture options only select a target environment."""
import argparse
import asyncio
from contextlib import nullcontext
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import sys
import uuid
from .config import Settings, validate_url


async def check_browser(settings):
    from playwright.async_api import async_playwright
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(settings.browsers_path)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='chromium', headless=True)
        await browser.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='通用三 Agent 网页测试：run --url URL --query 测试需求')
    parser.add_argument('command', nargs='?', default='run', choices=['run', 'check', 'serve', 'replay', 'resume', 'diagnose'])
    parser.add_argument('run_dir', nargs='?', type=Path)
    parser.add_argument('--url')
    parser.add_argument('--query')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--visible-input', action='store_true', help='仅用虚构数据时，允许不支持隐藏输入的控制台显示资源输入')
    parser.add_argument('--browser', action='store_true', help='check 时启动 Chromium')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--shop', action='store_true', help='商城环境快捷入口；仍使用通用三 Agent')
    parser.add_argument('--site', choices=['practice-login'], help='公开练习站网址快捷入口；不注入固定用例')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    settings = None
    try:
        settings = Settings.load(args.env_file)
        from .core.resources.interaction import ConsoleProvider
        provider = ConsoleProvider(visible_input=args.visible_input)
        if args.headed:
            settings = replace(settings, headless=False)
        if args.command == 'check':
            print(f'项目：{settings.root}\n解释器：{sys.executable}')
            print(f'DeepSeek：{"已配置" if settings.deepseek_api_key else "未配置"}；千问：{"已配置" if settings.qwen_api_key else "未配置"}')
            if args.browser:
                asyncio.run(check_browser(settings))
                print('Chromium 启动检查通过。')
            else:
                print('配置读取完成；未调用模型，也未启动浏览器。')
            return 0
        if args.command == 'serve':
            from .demo_sites import demo_server
            with demo_server(args.port) as url:
                print(f'独立测试环境：{url}；按 Ctrl+C 停止。', flush=True)
                import threading
                threading.Event().wait()
        if args.command in {'replay', 'resume', 'diagnose'}:
            if args.run_dir is None:
                raise ValueError('请指定已有运行目录。')
            directory = args.run_dir.expanduser().resolve()
            exported = directory if (directory / 'manifest.json').is_file() else directory / 'export'
            record = json.loads((directory / 'result.json').read_text(encoding='utf-8')) if (directory / 'result.json').is_file() else {}
            if args.command == 'replay':
                if not (exported / 'manifest.json').is_file():
                    raise ValueError('本次运行没有完整导出；请用 resume 从已确认计划起点重新执行。')
                from .core.delivery.launcher import launch
                result = asyncio.run(launch(exported, provider, timeout=settings.replay_timeout))
                print(f'独立重跑：{result["status"]}；证据：{exported / "replay.json"}')
                return 0 if result['status'] == 'passed' else 1
            if 'goal' not in record:
                raise ValueError('请指定当前版本的原运行目录，其中应包含 result.json；导出工程仅支持 replay。')
            if args.command == 'resume':
                if not record.get('plan'):
                    raise ValueError('尚无已确认计划，请用 run 重新开始。')
                from .core.workflow import run
                result = asyncio.run(run(settings, target_url=record['target_url'], goal=record['goal'],
                    frozen_plan=record['plan'], confirmed_answers=record.get('answers', {}),
                    confirmed_clarifications=record.get('clarifications', []), provider=provider))
            else:
                from .core.workflow import diagnose_saved
                result = asyncio.run(diagnose_saved(settings, directory))
            print(f'结果：{result["status"]}；新证据：{result.get("run_dir", result.get("directory"))}')
            return 0 if result['status'] in {'passed', 'completed'} else 1
        if sum(bool(v) for v in (args.url, args.shop, args.site)) > 1:
            raise ValueError('请选择一个目标网址或环境快捷入口。')
        query = args.query
        url = args.url
        context = nullcontext(None)
        if args.shop:
            from .sample_shop import shop_server
            context = shop_server()
        elif args.site:
            url = 'https://the-internet.herokuapp.com/login'
        elif not url:
            url = input('测试网址（回车使用独立测试环境）：').strip() or None
            if not url:
                from .demo_sites import demo_server
                context = demo_server()
        if not query:
            if args.url:
                raise ValueError('自定义网址请同时提供 --query 测试需求。')
            query = input('测试需求：').strip()
        if not query:
            raise ValueError('请输入测试需求，程序不会替你指定业务目标。')
        settings.require_model()
        from .core.workflow import run
        with context as fixture:
            if args.shop:
                base, store = fixture
                namespace = 'generic_' + uuid.uuid4().hex[:10]
                store.prepare(namespace)
                url = f'{base}/shop/{namespace}/login'
            else:
                url = url or fixture
            result = asyncio.run(run(settings, target_url=validate_url(url), goal=query, provider=provider))
        print(f'结果：{result["status"]}；报告：{Path(result["run_dir"]) / "report.html"}')
        if result.get('error'):
            print(result['error'][:1000])
        return 0 if result['status'] == 'passed' else 1
    except KeyboardInterrupt:
        print('已停止。')
        return 130
    except Exception as error:
        message = str(error) if settings is None else settings.redact(str(error))
        print(f'运行未完成（{type(error).__name__}）：{message}', file=sys.stderr)
        return 1
