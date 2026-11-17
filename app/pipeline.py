"""按区域授权领取、增量变更与定期全量核对的编排。

每个运行的顺序：
1. 授权范围核对：授权缩小时，受影响记录标为不可再确认（保留最后确认
   依据），绝不推断为已注销。
2. 增量变更：按作用域消费事件流。sequence 单调递增；重送的事件去重，
   缺段或游标失效则标记待恢复，转入全量核对。
3. 全量核对：对需要覆盖的区域按授权逐个分页领取。游标绑定来源版本与
   区域；只有游标走完的完整领取才提交，空页不等于全部退出。
4. 恢复收尾：全部授权区域都完成可解释的核对后，清除待恢复标记，缺段
   序列区间记入 sequence_skips，先前收据全部保留。
"""
import re
from datetime import datetime, timezone

from . import contract, delivery, ledger
from . import state as state_mod
from .contract import ContractError, CursorInvalid


class Notes:
    """单次运行的说明与计数，最终进入交付。"""

    def __init__(self):
        self.warnings = []
        self.events_applied = 0
        self.events_deduplicated = 0

    def warning(self, message):
        self.warnings.append(message)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def run(state, config, client, *, force_reconcile=False, clock=None):
    """执行一个采集运行，就地更新 state，返回交付说明。"""
    clock = clock or _utc_now
    state['run_id'] += 1
    notes = Notes()
    _apply_authorization(state, config, notes)
    _run_incremental(state, config, client, notes)
    for region in _reconcile_set(state, config, force_reconcile):
        _walk_region(state, config, client, region, notes)
    _maybe_clear_reconcile_flags(state, config, client, notes)
    return delivery.build(state, config, notes, clock)


# ---------------------------------------------------------------- 授权范围

def _apply_authorization(state, config, notes):
    """授权缩小时，旧记录标为不可再确认，而不是推断已注销。"""
    for record in state['dealers'].values():
        if (record['confirmable']
                and record['status'] not in contract.TERMINAL_STATUSES
                and record['region'] not in config.authorized_regions):
            record['confirmable'] = False
            record['unconfirmable_reason'] = 'region_not_authorized'
            notes.warning(
                f"经销商 {record['dealer_id']} 所在区域 {record['region']} "
                f'不在当前授权范围内，标为不可再确认（保留最后确认依据）')


# ---------------------------------------------------------------- 增量变更

def _run_incremental(state, config, client, notes):
    for scope_id in sorted(state['scopes']):
        scope = state['scopes'][scope_id]
        if scope.get('needs_reconcile'):
            continue
        cursor = None
        while True:
            try:
                response = client.fetch_changes(scope_id, scope['last_sequence'], cursor)
            except CursorInvalid as exc:
                _flag_reconcile(state, scope, reason='cursor_invalid', rebaseline=True)
                notes.warning(f'作用域 {scope_id} 变更流游标失效（{exc}），转入全量核对恢复')
                break
            except ContractError as exc:
                # 瞬时读取失败：流位置未变，本次跳过，下个运行重试
                notes.warning(f'作用域 {scope_id} 变更流读取失败（{exc}），本次跳过')
                break
            try:
                segment = contract.parse_change_segment(response.body)
            except ContractError as exc:
                _flag_reconcile(state, scope, reason='contract_error', rebaseline=True)
                notes.warning(f'作用域 {scope_id} 变更流违反契约（{exc}），转入全量核对恢复')
                break
            if segment.scope_id != scope_id:
                raise ContractError(
                    f'变更流作用域不匹配: 期望 {scope_id}，收到 {segment.scope_id}')
            receipt = ledger.segment_receipt(
                response, scope_id=scope_id, snapshot=segment.snapshot,
                first_sequence=segment.events[0].sequence if segment.events else None,
                last_sequence=segment.events[-1].sequence if segment.events else None,
                event_count=len(segment.events))
            state_mod.add_receipt(state, receipt)
            if _apply_segment(state, config, scope_id, scope, segment,
                              receipt['receipt_id'], notes) == 'gap':
                break
            if segment.next_cursor is None:
                break
            cursor = segment.next_cursor


def _apply_segment(state, config, scope_id, scope, segment, receipt_id, notes):
    """按序应用事件；重送去重，缺段标记待恢复。返回 'ok' 或 'gap'。"""
    for event in segment.events:
        if event.sequence <= scope['last_sequence']:
            notes.events_deduplicated += 1
            continue
        if event.sequence != scope['last_sequence'] + 1:
            _flag_reconcile(state, scope, reason='gap',
                            pending_skip_to=event.sequence - 1)
            notes.warning(
                f'作用域 {scope_id} 事件缺段: 期望 {scope["last_sequence"] + 1}，'
                f'收到 {event.sequence}，转入全量核对恢复')
            return 'gap'
        _apply_event(state, config, scope_id, event, receipt_id, notes)
        scope['last_sequence'] = event.sequence
        notes.events_applied += 1
    return 'ok'


def _apply_event(state, config, scope_id, event, receipt_id, notes):
    """应用单个事件。经销商身份只认 dealer_id，绝不按名称合并。"""
    basis = {'type': 'event', 'scope_id': scope_id, 'sequence': event.sequence,
             'kind': event.kind, 'receipt_id': receipt_id, 'run': state['run_id']}
    record = state['dealers'].get(event.dealer_id)
    if record is None:
        record = {'dealer_id': event.dealer_id, 'region': None, 'status': None,
                  'confirmable': True, 'unconfirmable_reason': None,
                  'merged_into': None, 'basis': None, 'last_confirmed': None}
        state['dealers'][event.dealer_id] = record
    if event.kind == 'transfer':
        if record['status'] in contract.TERMINAL_STATUSES:
            notes.warning(
                f"经销商 {event.dealer_id} 处于终止状态（{record['status']}）"
                f'仍收到转移事件，以来源事件为准更新区域')
        if (record['region'] and event.from_region
                and record['region'] != event.from_region):
            notes.warning(
                f"经销商 {event.dealer_id} 转移事件的来源区域（{event.from_region}）"
                f"与记录（{record['region']}）不一致")
        record['region'] = event.to_region
    elif event.kind == 'merge':
        record['status'] = 'merged'
        record['merged_into'] = event.target_dealer_id
    elif event.kind == 'revoke':
        record['status'] = 'revoked'
    record['basis'] = basis
    if record['status'] in contract.TERMINAL_STATUSES:
        record['confirmable'] = True
        record['unconfirmable_reason'] = None
    elif record['region'] not in config.authorized_regions:
        record['confirmable'] = False
        record['unconfirmable_reason'] = 'region_not_authorized'
    else:
        record['confirmable'] = True
        record['unconfirmable_reason'] = None
    if record['confirmable']:
        record['last_confirmed'] = {'region': record['region'],
                                    'status': record['status'], 'basis': basis}


def _flag_reconcile(state, scope, *, reason, pending_skip_to=None, rebaseline=False):
    scope['needs_reconcile'] = {'since_run': state['run_id'], 'reason': reason,
                                'pending_skip_to': pending_skip_to,
                                'rebaseline': rebaseline}


# ---------------------------------------------------------------- 全量核对

def _reconcile_set(state, config, force_reconcile):
    flagged = any(s.get('needs_reconcile') for s in state['scopes'].values())
    due = []
    for region in config.authorized_regions:
        coverage = state['coverage'].get(region)
        if (state['walks'].get(region) is not None or coverage is None
                or force_reconcile or flagged):
            due.append(region)
            continue
        interval = config.reconcile_interval_runs
        if interval and state['run_id'] - coverage['run'] >= interval:
            due.append(region)
    return due


def _walk_region(state, config, client, region, notes):
    """分页领取一个授权区域；游标失效则丢弃暂存并重新全量领取。"""
    restarts = 0
    while True:
        walk = state['walks'].get(region)
        if walk is None:
            walk = {'scope_id': None, 'snapshot': None, 'next_cursor': None,
                    'page_count': 0, 'staged': {}, 'receipt_ids': []}
            state['walks'][region] = walk
        try:
            _continue_walk(state, config, client, region, walk, notes)
            return
        except CursorInvalid as exc:
            restarts += 1
            notes.warning(f'区域 {region} 的分页游标失效（{exc}），丢弃暂存并重新全量领取')
            state['walks'].pop(region, None)
            if restarts > config.max_walk_restarts:
                notes.warning(f'区域 {region} 游标反复失效，本次全量核对未完成')
                return
        except ContractError as exc:
            notes.warning(f'区域 {region} 来源响应违反契约（{exc}），本次全量核对未完成')
            state['walks'].pop(region, None)
            return


def _continue_walk(state, config, client, region, walk, notes):
    fetched = 0
    while True:
        if fetched >= config.max_pages_per_walk:
            notes.warning(f'区域 {region} 达到单次运行领取页数上限，暂存已保留，下个运行继续')
            return
        response = client.fetch_directory(region, walk['next_cursor'])
        page = contract.parse_directory_page(response.body)
        if walk['scope_id'] is None:
            walk['scope_id'] = page.scope_id
            walk['snapshot'] = page.snapshot
        else:
            contract.check_page_binding(page, contract.CursorBinding(
                walk['scope_id'], walk['snapshot'], region, walk['next_cursor']))
        receipt = ledger.page_receipt(response, scope_id=page.scope_id,
                                      snapshot=page.snapshot, region=region,
                                      page_index=walk['page_count'],
                                      item_count=len(page.dealers))
        state_mod.add_receipt(state, receipt)
        walk['receipt_ids'].append(receipt['receipt_id'])
        for row in page.dealers:
            if row.region != region:
                raise ContractError(
                    f'目录页出现未授权领取区域的条目: {row.region}（领取区域 {region}）')
            if row.dealer_id in walk['staged']:
                raise ContractError(f'同一来源版本内经销商重复出现: {row.dealer_id}')
            walk['staged'][row.dealer_id] = {
                'dealer_id': row.dealer_id, 'region': row.region, 'status': row.status}
        walk['page_count'] += 1
        fetched += 1
        walk['next_cursor'] = page.next_cursor
        if page.next_cursor is None:
            _commit_walk(state, config, region, walk, notes)
            return


def _commit_walk(state, config, region, walk, notes):
    """提交一次完整领取。只有游标走完的领取才到达这里。"""
    scope_id = walk['scope_id']
    snapshot = walk['snapshot']
    scope = state['scopes'].setdefault(
        scope_id, {'last_sequence': 0, 'snapshot': None, 'needs_reconcile': None})
    previous = scope.get('snapshot')
    new_rank, old_rank = _version_rank(snapshot), _version_rank(previous)
    if new_rank is not None and old_rank is not None and new_rank < old_rank:
        notes.warning(
            f'区域 {region} 来源版本倒退（{previous} -> {snapshot}），'
            f'本次核对未采用，避免状态倒退')
        state['walks'].pop(region, None)
        return
    staged = walk['staged']
    for dealer_id in sorted(staged):
        row = staged[dealer_id]
        record = state['dealers'].get(dealer_id)
        if record is not None:
            if (record['status'] in contract.TERMINAL_STATUSES
                    and row['status'] not in contract.TERMINAL_STATUSES):
                notes.warning(
                    f'经销商 {dealer_id} 已有终止状态来源事件，'
                    f'快照重新出现但未据此恢复（等待来源事件）')
                continue
            if _fresh_event_basis(record, state):
                notes.warning(
                    f'经销商 {dealer_id} 本次运行已有更新的事件依据，'
                    f'快照可能早于该事件，保留事件依据')
                continue
        _confirm_record(state, dealer_id, region=row['region'], status=row['status'],
                        merged_into=None,
                        basis={'type': 'snapshot', 'scope_id': scope_id,
                               'snapshot': snapshot,
                               'receipt_ids': list(walk['receipt_ids'])})
    for record in state['dealers'].values():
        if (record['region'] == region and record['confirmable']
                and record['status'] not in contract.TERMINAL_STATUSES
                and record['dealer_id'] not in staged):
            if _fresh_event_basis(record, state):
                notes.warning(
                    f"经销商 {record['dealer_id']} 本次运行的事件依据新于快照，"
                    f'暂不标记缺席')
                continue
            record['confirmable'] = False
            record['unconfirmable_reason'] = 'absent_from_snapshot'
            notes.warning(
                f"经销商 {record['dealer_id']} 未出现在区域 {region} 的完整核对中，"
                f'标为不可再确认（而非已注销）')
    state['coverage'][region] = {'scope_id': scope_id, 'snapshot': snapshot,
                                 'pages': walk['page_count'], 'dealers': len(staged),
                                 'run': state['run_id'],
                                 'receipt_ids': list(walk['receipt_ids'])}
    scope['snapshot'] = snapshot
    state['walks'].pop(region, None)


def _confirm_record(state, dealer_id, *, region, status, merged_into, basis):
    state['dealers'][dealer_id] = {
        'dealer_id': dealer_id, 'region': region, 'status': status,
        'confirmable': True, 'unconfirmable_reason': None,
        'merged_into': merged_into, 'basis': basis,
        'last_confirmed': {'region': region, 'status': status, 'basis': basis}}


def _fresh_event_basis(record, state):
    """本次运行内应用的事件依据新于快照领取开始时间，快照可能早于它。"""
    basis = record.get('basis') or {}
    return basis.get('type') == 'event' and basis.get('run') == state['run_id']


def _version_rank(snapshot):
    if not isinstance(snapshot, str):
        return None
    match = re.fullmatch(r'v(\d+)', snapshot)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------- 恢复收尾

def _maybe_clear_reconcile_flags(state, config, client, notes):
    for scope_id in sorted(state['scopes']):
        scope = state['scopes'][scope_id]
        flag = scope.get('needs_reconcile')
        if not flag:
            continue
        covered = all(
            state['coverage'].get(r, {}).get('run', -1) >= flag['since_run']
            for r in config.authorized_regions)
        if not covered:
            continue
        skip_to = flag.get('pending_skip_to')
        if skip_to is not None and skip_to > scope['last_sequence']:
            state['sequence_skips'].append({
                'scope_id': scope_id, 'from': scope['last_sequence'] + 1,
                'to': skip_to, 'run': state['run_id'], 'reason': flag['reason']})
            scope['last_sequence'] = skip_to
            notes.warning(
                f'作用域 {scope_id} 缺段序列已由全量核对覆盖，序列推进至 {skip_to}')
        if flag.get('rebaseline') and not _rebaseline_scope(
                state, config, client, scope_id, scope, notes):
            continue
        scope['needs_reconcile'] = None


def _rebaseline_scope(state, config, client, scope_id, scope, notes):
    """变更流游标失效后的重基线：从来源保留起点重新对齐序列。"""
    try:
        response = client.fetch_changes(scope_id, '', None)
        segment = contract.parse_change_segment(response.body)
    except (ContractError, CursorInvalid) as exc:
        notes.warning(f'作用域 {scope_id} 变更流重基线失败: {exc}')
        return False
    if segment.scope_id != scope_id:
        raise ContractError(f'变更流作用域不匹配: 期望 {scope_id}，收到 {segment.scope_id}')
    receipt = ledger.segment_receipt(
        response, scope_id=scope_id, snapshot=segment.snapshot,
        first_sequence=segment.events[0].sequence if segment.events else None,
        last_sequence=segment.events[-1].sequence if segment.events else None,
        event_count=len(segment.events))
    state_mod.add_receipt(state, receipt)
    if segment.events:
        first = segment.events[0].sequence
        if first > scope['last_sequence'] + 1:
            state['sequence_skips'].append({
                'scope_id': scope_id, 'from': scope['last_sequence'] + 1,
                'to': first - 1, 'run': state['run_id'], 'reason': 'rebaseline'})
            scope['last_sequence'] = first - 1
            notes.warning(
                f'作用域 {scope_id} 变更流重基线：序列推进至 {first - 1}，'
                f'此前区间由全量核对覆盖')
    return _apply_segment(state, config, scope_id, scope, segment,
                          receipt['receipt_id'], notes) == 'ok'
