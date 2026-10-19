"""Markdown and HTML reports from actual execution evidence."""
from html import escape
from pathlib import Path
from ..contracts import Plan


def write_report(directory, state):
    directory = Path(directory)
    lines = ['# 网页测试报告', '', '状态：' + state['status'], '', state['goal'], '',
        '首次执行：' + state.get('generation_status', state['status']),
        '独立重跑：' + state.get('replay_result', {}).get('status', '未执行'),
        '完整脚本：' + ('已生成' if state.get('complete_script') else '未生成；保留部分执行证据'), '']
    if state.get('inconsistent'):
        lines += ['首次与重跑结果不一致，未标记通过。', '']
    if state.get('repaired'):
        lines += ['执行经过恢复；原始操作失败仍保留在记录中。', '']
    if state.get('error'):
        lines += ['原因：' + state['error'], '']
    if state.get('clarifications'):
        lines += ['## 需求澄清', '']
        for reply in state['clarifications']:
            lines += ['- 问：' + reply['text'], '  答：' + reply['answer']]
        lines.append('')
    plan = Plan.model_validate(state['plan']) if state.get('plan') else None
    if plan:
        for scenario in plan.scenarios:
            lines += ['## ' + scenario.objective, '']
            for checkpoint in scenario.checkpoints:
                for assertion in checkpoint.assertions:
                    matching = [e for e in state['records'] if e['kind'] == 'assertion' and e['scenario_id'] == scenario.id
                        and e['checkpoint_id'] == checkpoint.id and e['assertion_id'] == assertion.id]
                    status = matching[-1]['status'] if matching else 'not_run'
                    expected = ('resource:' + assertion.expected_resource_id) if assertion.expected_resource_id else repr(assertion.expected)
                    lines.append(f'- {assertion.target}：{assertion.operator} {expected} → {status}')
            lines.append('')
    for assessment in state['assessments']:
        lines += ['诊断：' + assessment['attribution'] + '；' + assessment['reason'], '']
    text = '\n'.join(lines)
    (directory / 'report.md').write_text(text, encoding='utf-8')
    (directory / 'report.html').write_text('<!doctype html><meta charset="utf-8"><title>网页测试报告</title><style>body{max-width:960px;margin:40px auto;font:16px/1.6 system-ui;padding:0 24px}pre{white-space:pre-wrap}</style><pre>' + escape(text) + '</pre>', encoding='utf-8')
