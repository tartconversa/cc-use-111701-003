"""来源契约解析：目录分页、变更流段与分页游标绑定。

目录 JSON：scope_id、snapshot、next_cursor、dealers[{dealer_id,region,status}]。
变更流段：scope_id、snapshot、next_cursor、events[{sequence,kind,dealer_id,
from_region,to_region,target_dealer_id?}]；sequence 在 scope 内递增，事件可以重送。

分页游标绑定来源版本（snapshot）与区域：一次领取中途来源版本变化即视为
游标失效，调用方应丢弃暂存并重新开始该区域的全量领取。
"""
import json
from dataclasses import dataclass

EVENT_KINDS = ('transfer', 'merge', 'revoke')
TERMINAL_STATUSES = ('merged', 'revoked')


class ContractError(ValueError):
    """来源响应违反契约。"""


class CursorInvalid(ContractError):
    """分页游标失效或与其绑定的来源版本、区域不匹配。"""


@dataclass(frozen=True)
class DealerRow:
    dealer_id: str
    region: str
    status: str


@dataclass(frozen=True)
class DirectoryPage:
    scope_id: str
    snapshot: str
    next_cursor: str | None
    dealers: tuple[DealerRow, ...]


@dataclass(frozen=True)
class ChangeEvent:
    sequence: int
    kind: str
    dealer_id: str
    from_region: str | None
    to_region: str | None
    target_dealer_id: str | None


@dataclass(frozen=True)
class ChangeSegment:
    scope_id: str
    snapshot: str | None
    next_cursor: str | None
    events: tuple[ChangeEvent, ...]


@dataclass(frozen=True)
class CursorBinding:
    """分页游标的绑定：来源版本与区域（目录）或作用域（变更流）。"""
    scope_id: str
    snapshot: str
    region: str | None
    value: str | None


def _require_str(value, field):
    if not isinstance(value, str) or not value:
        raise ContractError(f'字段无效或缺失: {field}')
    return value


def _optional_str(value, field):
    if value is None:
        return None
    return _require_str(value, field)


def _parse_body(body):
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError(f'来源实体不是有效 JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise ContractError('来源实体必须是 JSON 对象')
    return value


def _parse_next_cursor(value):
    cursor = value.get('next_cursor')
    if cursor is not None:
        cursor = _require_str(cursor, 'next_cursor')
    return cursor


def parse_directory_page(body) -> DirectoryPage:
    value = _parse_body(body)
    scope_id = _require_str(value.get('scope_id'), 'scope_id')
    snapshot = _require_str(value.get('snapshot'), 'snapshot')
    next_cursor = _parse_next_cursor(value)
    rows = value.get('dealers')
    if not isinstance(rows, list):
        raise ContractError('缺少 dealers 数组')
    dealers = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError('dealers 条目必须是对象')
        dealer_id = _require_str(row.get('dealer_id'), 'dealer_id')
        region = _require_str(row.get('region'), 'region')
        status = _require_str(row.get('status'), 'status')
        if dealer_id in seen:
            raise ContractError(f'同一页内经销商重复: {dealer_id}')
        seen.add(dealer_id)
        dealers.append(DealerRow(dealer_id, region, status))
    return DirectoryPage(scope_id, snapshot, next_cursor, tuple(dealers))


def parse_change_segment(body) -> ChangeSegment:
    value = _parse_body(body)
    scope_id = _require_str(value.get('scope_id'), 'scope_id')
    snapshot = _optional_str(value.get('snapshot'), 'snapshot')
    next_cursor = _parse_next_cursor(value)
    raw_events = value.get('events')
    if not isinstance(raw_events, list):
        raise ContractError('缺少 events 数组')
    events = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise ContractError('events 条目必须是对象')
        sequence = raw.get('sequence')
        if type(sequence) is not int or sequence < 1:
            raise ContractError('事件 sequence 无效')
        kind = _require_str(raw.get('kind'), 'kind')
        if kind not in EVENT_KINDS:
            raise ContractError(f'未知事件类型: {kind}')
        dealer_id = _require_str(raw.get('dealer_id'), 'dealer_id')
        from_region = _optional_str(raw.get('from_region'), 'from_region')
        to_region = _optional_str(raw.get('to_region'), 'to_region')
        target = _optional_str(raw.get('target_dealer_id'), 'target_dealer_id')
        if kind == 'transfer' and (from_region is None or to_region is None):
            raise ContractError('转移事件缺少 from_region 或 to_region')
        events.append(ChangeEvent(sequence, kind, dealer_id, from_region, to_region, target))
    return ChangeSegment(scope_id, snapshot, next_cursor, tuple(events))


def check_page_binding(page: DirectoryPage, binding: CursorBinding):
    """校验分页响应与游标绑定的来源版本和区域一致。"""
    if page.scope_id != binding.scope_id or page.snapshot != binding.snapshot:
        raise CursorInvalid(
            f'分页中途来源版本变化: ({binding.scope_id},{binding.snapshot}) -> '
            f'({page.scope_id},{page.snapshot})')
