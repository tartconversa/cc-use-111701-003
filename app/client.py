"""来源访问客户端。

客户端协议（pipeline 只依赖这两个方法）：
- fetch_directory(region, cursor) -> source.Response
- fetch_changes(scope_id, after, cursor) -> source.Response

UrlClient 用配置里的 URL 模板访问真实来源；FixtureClient 从本地 JSON
文件回放响应，供离线演示与测试。两者都把 HTTP 409/410 映射为
CursorInvalid（游标失效），把其他非 200 映射为 ContractError。
"""
import json
from pathlib import Path
from urllib.parse import quote

from .contract import ContractError, CursorInvalid
from .source import Response, fetch

_CURSOR_REJECTED = (409, 410)


def ensure_ok(response: Response) -> Response:
    if response.status in _CURSOR_REJECTED:
        raise CursorInvalid(f'来源拒绝游标: HTTP {response.status}')
    if response.status != 200:
        raise ContractError(f'来源未返回成功实体: HTTP {response.status}')
    return response


class UrlClient:
    """按 URL 模板访问来源。模板占位符：
    目录 {region}、{cursor}；变更流 {scope}、{after}、{cursor}。"""

    def __init__(self, config, *, max_bytes=1048576):
        if not config.directory_url or not config.changes_url:
            raise ValueError('配置缺少 directory_url 或 changes_url 模板')
        self._config = config
        self._max_bytes = max_bytes

    def fetch_directory(self, region, cursor):
        url = self._config.directory_url.format(
            region=quote(region, safe=''), cursor=quote(cursor or '', safe=''))
        return ensure_ok(fetch(url, self._config.source_id, max_bytes=self._max_bytes))

    def fetch_changes(self, scope_id, after, cursor):
        url = self._config.changes_url.format(
            scope=quote(scope_id, safe=''), after=after,
            cursor=quote(cursor or '', safe=''))
        return ensure_ok(fetch(url, self._config.source_id, max_bytes=self._max_bytes))


class FixtureClient:
    """从本地 JSON 文件回放来源响应。

    文件形如 {"directory": {"华中|": {...}, "华中|cursor-a": {...}},
              "changes": {"central|0|": {...}}}
    键为 区域|游标 与 作用域|起始序列|游标；值为来源响应实体，
    或 {"__status__": 409} 表示该游标已失效。
    """

    def __init__(self, path, *, source_id='source-a'):
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('fixtures 文件必须是 JSON 对象')
        self._directory = value.get('directory', {})
        self._changes = value.get('changes', {})
        self._source_id = source_id

    def _respond(self, table, key):
        if key not in table:
            raise ContractError(f'fixtures 缺少响应: {key}')
        body = table[key]
        status = 200
        if isinstance(body, dict) and '__status__' in body:
            status = body['__status__']
            body = body.get('body', {})
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        response = Response(self._source_id, f'fixture://{key}', status,
                            (('Content-Type', 'application/json'),), payload)
        return ensure_ok(response)

    def fetch_directory(self, region, cursor):
        return self._respond(self._directory, f'{region}|{cursor or ""}')

    def fetch_changes(self, scope_id, after, cursor):
        return self._respond(self._changes, f'{scope_id}|{after}|{cursor or ""}')
