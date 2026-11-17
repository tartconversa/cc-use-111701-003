"""跨运行持久化的名册状态与收据日志。

状态内容：
- scopes: 每个作用域的 last_sequence、当前来源版本、待恢复标记
  （needs_reconcile: since_run/reason/pending_skip_to/rebaseline）。
- walks: 每个区域进行中的分页领取（游标绑定、暂存条目、已收收据），
  重启后从游标继续，而不是从头重取或把半成品当成全量。
- coverage: 每个区域最近一次完整核对的来源版本、页数与收据。
- dealers: 每个经销商的当前记录。status/region 只来自来源依据
  （快照或事件）；confirmable=False 表示不可再确认，绝不推断为已注销。
- receipts: 只增不减的收据日志，先前收据在恢复后仍然保留。
- sequence_skips: 缺段由全量核对覆盖后跳过的序列区间，供审计。
"""
import json
from pathlib import Path

STATE_VERSION = 1


class StateError(ValueError):
    """状态文件不可解析或版本不受支持；绝不静默丢弃先前收据。"""


def empty_state():
    return {
        'version': STATE_VERSION,
        'run_id': 0,
        'scopes': {},
        'walks': {},
        'coverage': {},
        'dealers': {},
        'receipts': [],
        'sequence_skips': [],
    }


def load(path):
    p = Path(path)
    if not p.exists():
        return empty_state()
    try:
        value = json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StateError(f'状态文件不可解析: {exc}') from exc
    if not isinstance(value, dict) or value.get('version') != STATE_VERSION:
        raise StateError('状态文件版本不受支持')
    if type(value.get('run_id')) is not int:
        raise StateError('状态字段无效: run_id')
    for key in ('scopes', 'walks', 'coverage', 'dealers'):
        if not isinstance(value.get(key), dict):
            raise StateError(f'状态字段无效: {key}')
    for key in ('receipts', 'sequence_skips'):
        if not isinstance(value.get(key), list):
            raise StateError(f'状态字段无效: {key}')
    return value


def save(path, state):
    Path(path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')


def add_receipt(state, receipt):
    """按 receipt_id 去重追加；重送的响应不会产生重复收据。"""
    if any(r['receipt_id'] == receipt['receipt_id'] for r in state['receipts']):
        return False
    state['receipts'].append(receipt)
    return True
