"""Generic human input and redaction, independent of resource types and demos."""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import getpass
import json
import os
import queue
import re
import sys
import threading
import time
import warnings
from urllib.parse import quote, quote_plus


class ResourceUnavailable(RuntimeError):
    status = 'blocked'


class ResourceCancelled(ResourceUnavailable):
    status = 'cancelled'


@dataclass(frozen=True)
class ResourceRequest:
    id: str
    ref: str
    kind: str
    purpose: str
    scene_id: str
    step_id: str
    run_id: str
    sensitive: bool = True
    reusable: bool = False
    wait_timeout: float = 120

    def audit(self):
        return asdict(self)


class ActiveClock:
    def __init__(self):
        self.started, self.wait_seconds, self.wait_started = time.monotonic(), 0., None

    @property
    def active_seconds(self):
        return (self.wait_started or time.monotonic()) - self.started - self.wait_seconds

    @contextmanager
    def waiting(self):
        if self.wait_started is not None:
            raise RuntimeError('Only one input owner may wait')
        self.wait_started = time.monotonic()
        try:
            yield
        finally:
            self.wait_seconds += time.monotonic() - self.wait_started
            self.wait_started = None


class ConsoleProvider:
    def __init__(self, *, visible_input=False):
        self.visible_input, self.closed = visible_input, False
        self.lock = asyncio.Lock()

    async def acquire(self, request):
        async with self.lock:
            available = sys.stdin is not None and not sys.stdin.closed
            tty = available and sys.stdin.isatty()
            hidden = request.sensitive and not self.visible_input
            hidden_help = ('当前控制台无法完成隐藏输入。真实敏感数据请使用 PowerShell / Windows Terminal；'
                           '测试虚构数据时，请重新运行并添加 --visible-input。')
            if self.closed or not available or not (tty or os.environ.get('PYCHARM_HOSTED') == '1' or self.visible_input):
                raise ResourceUnavailable('当前控制台无法接收输入，请在可交互终端重新运行；虚构数据可添加 --visible-input。')
            if hidden and not tty:
                raise ResourceUnavailable(hidden_help)
            print(f'需要 {request.kind}：{request.purpose}；直接回车发送，:cancel 取消。', flush=True)
            if hidden:
                print('隐藏输入：键入时不显示字符。IDE 控制台若无法输入，虚构数据请加 --visible-input 后重新运行。', flush=True)
            else:
                print('文本输入：键入的字符会显示在控制台。', flush=True)
            replies = queue.Queue(maxsize=1)
            def read():
                try:
                    if hidden:
                        with warnings.catch_warnings():
                            # Never let getpass silently fall back to echoing a secret.
                            warnings.simplefilter('error', getpass.GetPassWarning)
                            value = getpass.getpass('输入：')
                    else:
                        value = input('输入：')
                    replies.put((True, value))
                except KeyboardInterrupt:
                    replies.put((False, 'KeyboardInterrupt'))
                except BaseException as error:
                    # Error text can include input; retain only the exception type.
                    replies.put((False, type(error).__name__))
            threading.Thread(target=read, daemon=True, name='human-input-owner').start()
            deadline = time.monotonic() + request.wait_timeout
            try:
                while time.monotonic() < deadline:
                    try:
                        success, value = replies.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(.05)
                        continue
                    if not success:
                        if value == 'KeyboardInterrupt':
                            raise ResourceCancelled('User cancelled input')
                        if hidden:
                            raise ResourceUnavailable(f'{hidden_help}（{value}）')
                        raise ResourceUnavailable(f'控制台输入已关闭（{value}），请在可交互终端重新运行。')
                    if value.strip() == ':cancel':
                        raise ResourceCancelled('User cancelled input')
                    return value
                raise ResourceUnavailable('Input deadline exceeded')
            except BaseException:
                self.closed = True
                raise


class Redactor:
    _metadata = {
        'status': {'pending', 'running', 'waiting_user', 'provided', 'passed', 'failed', 'error', 'blocked', 'cancelled', 'action', 'resource', 'verify', 'replan', 'inspect', 'clarify', 'ready'},
        'kind': {'navigate', 'click', 'fill', 'select', 'check', 'uncheck', 'press', 'wait', 'action', 'resource', 'assertion', 'scenario_start', 'replay', 'decision_rejected'},
        'operator': {'visible', 'hidden', 'text', 'contains', 'value', 'count', 'url', 'checked', 'enabled'},
        'tag': {'input', 'textarea', 'select', 'button', 'a', 'h1', 'h2', 'h3', 'p', 'output', 'span', 'div'},
        'type': {'', 'text', 'password', 'email', 'number', 'tel', 'checkbox', 'radio', 'submit', 'button', 'search', 'url'},
        'role': {'', 'button', 'link', 'status', 'alert', 'heading', 'checkbox', 'radio', 'textbox', 'combobox'},
    }

    def __init__(self):
        self._variants = set()
        self.public_ids = set()

    def register(self, value):
        if value:
            self._variants.update((value, quote(value, safe=''), quote_plus(value),
                                  json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1]))

    def text(self, value):
        for secret in sorted(self._variants, key=lambda item: (-len(item), item)):
            value = value.replace(secret, '[RESOURCE]')
        return value

    def data(self, value, field=None):
        if isinstance(value, str):
            # Owned protocol vocabulary is not a resource value. Masking a password
            # equal to "passed" must not turn an actual tool status into an error.
            if value in self._metadata.get(field, set()):
                return value
            if field in {'id', 'ref', 'resource_id', 'expected_resource_id', 'assertion_id', 'requirement_id', 'checkpoint_id', 'scenario_id', 'source'} and value in self.public_ids:
                return value
            if field in {'id', 'ref', 'target'} and re.fullmatch(r'(node|ev)-\d+', value):
                return value
            return self.text(value)
        if isinstance(value, dict):
            return {key: self.data(item, key) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.data(item, field) for item in value]
        return value

    def clear(self):
        self._variants.clear()
