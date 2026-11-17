"""本地授权与采集配置。

授权区域从本地配置给出；来源 URL 模板与真实凭据由运行时提供。
reconcile_interval_runs 为 0 表示不做周期性全量核对，仅按需
（首次覆盖、缺段恢复、游标失效恢复或强制）进行。
"""
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_PAGES_PER_WALK = 1000
DEFAULT_MAX_WALK_RESTARTS = 3


@dataclass(frozen=True)
class Config:
    source_id: str
    authorized_regions: tuple[str, ...]
    directory_url: str | None = None
    changes_url: str | None = None
    reconcile_interval_runs: int = 0
    max_pages_per_walk: int = DEFAULT_MAX_PAGES_PER_WALK
    max_walk_restarts: int = DEFAULT_MAX_WALK_RESTARTS


def _require_str(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f'配置字段无效: {field}')
    return value


def _optional_str(value, field):
    if value is None:
        return None
    return _require_str(value, field)


def _require_int(value, field, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f'配置字段无效: {field}')
    return value


def parse_config(value) -> Config:
    if not isinstance(value, dict):
        raise ValueError('配置必须是 JSON 对象')
    source_id = _require_str(value.get('source_id'), 'source_id')
    regions = value.get('authorized_regions')
    if (not isinstance(regions, list) or not regions
            or any(not isinstance(r, str) or not r for r in regions)):
        raise ValueError('配置字段无效: authorized_regions')
    if len(set(regions)) != len(regions):
        raise ValueError('授权区域重复')
    return Config(
        source_id=source_id,
        authorized_regions=tuple(regions),
        directory_url=_optional_str(value.get('directory_url'), 'directory_url'),
        changes_url=_optional_str(value.get('changes_url'), 'changes_url'),
        reconcile_interval_runs=_require_int(
            value.get('reconcile_interval_runs', 0), 'reconcile_interval_runs', 0),
        max_pages_per_walk=_require_int(
            value.get('max_pages_per_walk', DEFAULT_MAX_PAGES_PER_WALK),
            'max_pages_per_walk', 1),
        max_walk_restarts=_require_int(
            value.get('max_walk_restarts', DEFAULT_MAX_WALK_RESTARTS),
            'max_walk_restarts', 0),
    )


def load_config(path) -> Config:
    return parse_config(json.loads(Path(path).read_text(encoding='utf-8')))
