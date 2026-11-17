"""按区域授权领取、增量变更与全量核对的测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from app import state as state_mod
from app.client import ensure_ok
from app.config import parse_config
from app.contract import (
    ContractError, CursorInvalid, parse_change_segment, parse_directory_page)
from app.pipeline import run
from app.service import run_pipeline
from app.source import Response
from app.state import empty_state

CLOCK = lambda: '2026-11-18T00:00:00+00:00'
ROOT = Path(__file__).resolve().parents[1]


def make_config(**overrides):
    base = {'source_id': 'source-a', 'authorized_regions': ['华中', '华南']}
    base.update(overrides)
    return parse_config(base)


def page(scope, snapshot, next_cursor, dealers):
    return {'scope_id': scope, 'snapshot': snapshot,
            'next_cursor': next_cursor, 'dealers': dealers}


def dealer(dealer_id, region, status='active', **extra):
    row = {'dealer_id': dealer_id, 'region': region, 'status': status}
    row.update(extra)
    return row


def segment(scope, events, snapshot='v1', next_cursor=None):
    return {'scope_id': scope, 'snapshot': snapshot,
            'next_cursor': next_cursor, 'events': events}


def ev(seq, kind, dealer_id, **extra):
    event = {'sequence': seq, 'kind': kind, 'dealer_id': dealer_id}
    event.update(extra)
    return event


class FakeClient:
    """按调用键回放响应；列表值表示连续调用依次返回。"""

    def __init__(self, directory=None, changes=None):
        self.directory = dict(directory or {})
        self.changes = dict(changes or {})
        self.directory_calls = []
        self.changes_calls = []

    def fetch_directory(self, region, cursor):
        key = f'{region}|{cursor or ""}'
        self.directory_calls.append(key)
        return self._respond(self.directory, key, self.directory_calls)

    def fetch_changes(self, scope_id, after, cursor):
        key = f'{scope_id}|{after}|{cursor or ""}'
        self.changes_calls.append(key)
        if key not in self.changes:
            # 变更流没有新事件是正常情况，默认返回空段
            body = {'scope_id': scope_id, 'snapshot': None,
                    'next_cursor': None, 'events': []}
            return ensure_ok(Response(
                'source-a', f'fake://{key}', 200,
                (('Content-Type', 'application/json'),),
                json.dumps(body).encode('utf-8')))
        return self._respond(self.changes, key, self.changes_calls)

    def _respond(self, table, key, calls):
        if key not in table:
            raise ContractError(f'fake 无响应: {key}')
        value = table[key]
        if isinstance(value, list):
            value = value[min(calls.count(key) - 1, len(value) - 1)]
        if isinstance(value, Exception):
            raise value
        status, body = 200, value
        if isinstance(value, dict) and '__status__' in value:
            status, body = value['__status__'], value.get('body', {})
        response = Response('source-a', f'fake://{key}', status,
                            (('Content-Type', 'application/json'),),
                            json.dumps(body, ensure_ascii=False).encode('utf-8'))
        return ensure_ok(response)


def seed_state(directory=None):
    """一个运行的初始全量覆盖。"""
    state = empty_state()
    client = FakeClient(directory=directory or {
        '华中|': page('central', 'v1', None,
                      [dealer('d-1', '华中'), dealer('d-2', '华中')]),
        '华南|': page('central', 'v1', None, [dealer('d-7', '华南')]),
    })
    run(state, make_config(), client, clock=CLOCK)
    return state


def dealers_of(result):
    return {d['dealer_id']: d for d in result['dealers']}


class PaginationTest(unittest.TestCase):
    def test_full_pagination_completes_coverage(self):
        client = FakeClient(directory={
            '华中|': page('central', 'v1', 'h2',
                          [dealer('d-1', '华中'), dealer('d-2', '华中')]),
            '华中|h2': page('central', 'v1', None, [dealer('d-3', '华中')]),
            '华南|': page('central', 'v1', None, [dealer('d-7', '华南')]),
        })
        result = run(empty_state(), make_config(), client, clock=CLOCK)
        coverage = {c['region']: c for c in result['coverage']}
        self.assertEqual(coverage['华中']['snapshot'], 'v1')
        self.assertEqual(coverage['华中']['pages'], 2)
        self.assertEqual(coverage['华中']['dealers'], 3)
        self.assertEqual(result['uncovered_regions'], [])
        dealers = dealers_of(result)
        for dealer_id in ('d-1', 'd-2', 'd-3', 'd-7'):
            self.assertTrue(dealers[dealer_id]['confirmable'])
        self.assertEqual(dealers['d-1']['basis']['type'], 'snapshot')
        self.assertEqual(dealers['d-1']['basis']['snapshot'], 'v1')

    def test_empty_first_page_with_cursor_is_not_complete(self):
        # 空首页带游标：必须继续翻页，不能当成全部退出
        client = FakeClient(directory={
            '华中|': page('central', 'v1', 'h2', []),
            '华中|h2': page('central', 'v1', None, [dealer('d-1', '华中')]),
            '华南|': page('central', 'v1', None, []),
        })
        result = run(empty_state(), make_config(), client, clock=CLOCK)
        self.assertEqual(client.directory_calls.count('华中|h2'), 1)
        self.assertTrue(dealers_of(result)['d-1']['confirmable'])

    def test_empty_final_page_is_complete_and_keeps_dealers(self):
        client = FakeClient(directory={
            '华中|': page('central', 'v1', 'h2', [dealer('d-1', '华中')]),
            '华中|h2': page('central', 'v1', None, []),
            '华南|': page('central', 'v1', None, []),
        })
        result = run(empty_state(), make_config(), client, clock=CLOCK)
        self.assertTrue(dealers_of(result)['d-1']['confirmable'])
        coverage = {c['region']: c for c in result['coverage']}
        self.assertEqual(coverage['华中']['pages'], 2)

    def test_incomplete_walk_marks_nothing_absent(self):
        state = seed_state()
        # 强制核对中华东第一页后来源中断：半成品不得提交，旧记录不得误标
        client = FakeClient(directory={
            '华中|': page('central', 'v2', 'h2', [dealer('d-1', '华中')]),
            '华中|h2': ContractError('来源中断'),
            '华南|': page('central', 'v2', None, []),
        })
        result = run(state, make_config(), client,
                     force_reconcile=True, clock=CLOCK)
        dealers = dealers_of(result)
        self.assertTrue(dealers['d-2']['confirmable'])
        self.assertTrue(any('来源中断' in w for w in result['warnings']))
        coverage = {c['region']: c for c in result['coverage']}
        self.assertEqual(coverage['华中']['snapshot'], 'v1')  # 旧覆盖保留

    def test_cursor_binding_snapshot_mismatch_restarts_walk(self):
        client = FakeClient(directory={
            '华中|': [page('central', 'v1', 'h1', [dealer('d-1', '华中')]),
                      page('central', 'v2', 'h2', [dealer('d-1', '华中')])],
            '华中|h1': page('central', 'v2', None, []),  # 版本中途变化
            '华中|h2': page('central', 'v2', None, [dealer('d-2', '华中')]),
            '华南|': page('central', 'v2', None, []),
        })
        state = empty_state()
        result = run(state, make_config(), client, clock=CLOCK)
        self.assertTrue(any('游标失效' in w for w in result['warnings']))
        coverage = {c['region']: c for c in result['coverage']}
        self.assertEqual(coverage['华中']['snapshot'], 'v2')
        dealers = dealers_of(result)
        self.assertTrue(dealers['d-2']['confirmable'])
        # 先前收据（含 v1 那一页）全部保留
        self.assertTrue(any(r['snapshot'] == 'v1' for r in state['receipts']
                            if r['type'] == 'directory_page'))

    def test_restart_mid_walk_resumes_from_cursor(self):
        config = make_config(max_pages_per_walk=1)
        client = FakeClient(directory={
            '华中|': page('central', 'v1', 'h2', [dealer('d-1', '华中')]),
            '华中|h2': page('central', 'v1', None, [dealer('d-2', '华中')]),
            '华南|': page('central', 'v1', None, []),
        })
        state = empty_state()
        result1 = run(state, config, client, clock=CLOCK)
        self.assertIn('华中', state['walks'])
        self.assertEqual(state['walks']['华中']['next_cursor'], 'h2')
        self.assertNotIn('d-1', dealers_of(result1))  # 半成品不提交
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            state_mod.save(path, state)
            state2 = state_mod.load(path)
        result2 = run(state2, config, client, clock=CLOCK)
        self.assertNotIn('华中', state2['walks'])
        dealers = dealers_of(result2)
        self.assertTrue(dealers['d-1']['confirmable'])
        self.assertTrue(dealers['d-2']['confirmable'])
        self.assertEqual(client.directory_calls.count('华中|'), 1)  # 首页未重取

    def test_snapshot_version_regression_is_rejected(self):
        state = empty_state()
        run(state, make_config(), FakeClient(directory={
            '华中|': page('central', 'v3', None, [dealer('d-1', '华中')]),
            '华南|': page('central', 'v3', None, []),
        }), clock=CLOCK)
        result = run(state, make_config(), FakeClient(directory={
            '华中|': page('central', 'v2', None, [dealer('d-9', '华中')]),
            '华南|': page('central', 'v2', None, []),
        }), force_reconcile=True, clock=CLOCK)
        coverage = {c['region']: c for c in result['coverage']}
        self.assertEqual(coverage['华中']['snapshot'], 'v3')
        dealers = dealers_of(result)
        self.assertNotIn('d-9', dealers)
        self.assertTrue(dealers['d-1']['confirmable'])
        self.assertTrue(any('倒退' in w for w in result['warnings']))

    def test_duplicate_dealer_in_one_snapshot_fails_walk(self):
        client = FakeClient(directory={
            '华中|': page('central', 'v1', 'h2', [dealer('d-1', '华中')]),
            '华中|h2': page('central', 'v1', None, [dealer('d-1', '华中')]),
            '华南|': page('central', 'v1', None, []),
        })
        result = run(empty_state(), make_config(), client, clock=CLOCK)
        self.assertTrue(any('重复' in w for w in result['warnings']))
        self.assertEqual({c['region'] for c in result['coverage']}, {'华南'})

    def test_periodic_reconcile_interval(self):
        config = make_config(reconcile_interval_runs=2)
        client = FakeClient(directory={
            '华中|': page('central', 'v1', None, [dealer('d-1', '华中')]),
            '华南|': page('central', 'v1', None, []),
        })
        state = empty_state()
        run(state, config, client, clock=CLOCK)
        run(state, config, client, clock=CLOCK)
        self.assertEqual(client.directory_calls.count('华中|'), 1)
        run(state, config, client, clock=CLOCK)
        self.assertEqual(client.directory_calls.count('华中|'), 2)


class EventTest(unittest.TestCase):
    def test_transfer_merge_revoke_events(self):
        state = seed_state()
        client = FakeClient(changes={'central|0|': segment('central', [
            ev(1, 'transfer', 'd-1', from_region='华中', to_region='华南'),
            ev(2, 'merge', 'd-2', target_dealer_id='d-7'),
            ev(3, 'revoke', 'd-7'),
        ])})
        result = run(state, make_config(), client, clock=CLOCK)
        dealers = dealers_of(result)
        self.assertEqual(dealers['d-1']['region'], '华南')
        self.assertEqual(dealers['d-1']['basis']['type'], 'event')
        self.assertEqual(dealers['d-1']['basis']['sequence'], 1)
        self.assertEqual(dealers['d-2']['status'], 'merged')
        self.assertEqual(dealers['d-2']['merged_into'], 'd-7')
        self.assertEqual(dealers['d-7']['status'], 'revoked')
        self.assertEqual(result['summary']['events_applied'], 3)
        self.assertEqual(state['scopes']['central']['last_sequence'], 3)

    def test_no_name_based_merging(self):
        # 名称相同不是身份：合并事件只影响事件指名的 dealer_id
        state = seed_state(directory={
            '华中|': page('central', 'v1', None, [
                dealer('d-2', '华中', name='旗舰店'),
                dealer('d-3', '华中', name='旗舰店')]),
            '华南|': page('central', 'v1', None, []),
        })
        result = run(state, make_config(), FakeClient(changes={
            'central|0|': segment('central', [
                ev(1, 'merge', 'd-2', target_dealer_id='d-9')])}), clock=CLOCK)
        dealers = dealers_of(result)
        self.assertEqual(dealers['d-2']['status'], 'merged')
        self.assertEqual(dealers['d-3']['status'], 'active')
        self.assertTrue(dealers['d-3']['confirmable'])

    def test_transfer_out_of_authorized_regions_is_unconfirmable(self):
        state = seed_state()
        result = run(state, make_config(), FakeClient(changes={
            'central|0|': segment('central', [
                ev(1, 'transfer', 'd-1', from_region='华中', to_region='西南')])}),
            clock=CLOCK)
        dealers = dealers_of(result)
        self.assertFalse(dealers['d-1']['confirmable'])
        self.assertEqual(dealers['d-1']['unconfirmable_reason'],
                         'region_not_authorized')
        self.assertEqual(dealers['d-1']['status'], 'active')  # 不推断注销
        self.assertEqual(dealers['d-1']['region'], '西南')  # 事件如实记录

    def test_resent_events_do_not_duplicate_or_regress(self):
        state = seed_state()
        changes = {'central|0|': segment('central', [
            ev(1, 'revoke', 'd-1'),
            ev(2, 'transfer', 'd-2', from_region='华中', to_region='华南')])}
        result1 = run(state, make_config(), FakeClient(changes=changes), clock=CLOCK)
        self.assertEqual(result1['summary']['events_applied'], 2)
        receipts_after_run1 = len(state['receipts'])
        # 重启：状态落盘再读回；来源重送同样的事件（忽略 after）
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            state_mod.save(path, state)
            state2 = state_mod.load(path)
        resend = {'central|2|': segment('central', [
            ev(1, 'revoke', 'd-1'),
            ev(2, 'transfer', 'd-2', from_region='华中', to_region='华南')])}
        result2 = run(state2, make_config(), FakeClient(changes=resend), clock=CLOCK)
        self.assertEqual(result2['summary']['events_applied'], 0)
        self.assertEqual(result2['summary']['events_deduplicated'], 2)
        self.assertEqual(state2['scopes']['central']['last_sequence'], 2)
        self.assertEqual(len(state2['receipts']), receipts_after_run1)
        dealers = dealers_of(result2)
        self.assertEqual(dealers['d-1']['status'], 'revoked')
        self.assertEqual(dealers['d-2']['region'], '华南')

    def test_gap_triggers_explainable_reconcile_then_resumes(self):
        state = seed_state()
        run(state, make_config(), FakeClient(changes={
            'central|0|': segment('central', [ev(1, 'revoke', 'd-1')])}),
            clock=CLOCK)
        self.assertEqual(state['scopes']['central']['last_sequence'], 1)
        # 缺段：期望 2，收到 4
        client = FakeClient(
            directory={
                '华中|': page('central', 'v2', None, [dealer('d-2', '华中')]),
                '华南|': page('central', 'v2', None, [dealer('d-7', '华南')]),
            },
            changes={'central|1|': segment('central', [ev(4, 'revoke', 'd-7')],
                                           snapshot='v2')})
        result = run(state, make_config(), client, clock=CLOCK)
        scope = state['scopes']['central']
        self.assertTrue(any('缺段' in w for w in result['warnings']))
        self.assertIsNone(scope['needs_reconcile'])  # 全量核对后已恢复
        self.assertEqual(scope['last_sequence'], 3)  # 缺段区间被跳过
        self.assertEqual(state['sequence_skips'][0]['from'], 2)
        self.assertEqual(state['sequence_skips'][0]['to'], 3)
        dealers = dealers_of(result)
        self.assertEqual(dealers['d-7']['status'], 'active')  # 事件 4 尚未应用
        coverage = {c['region']: c for c in result['coverage']}
        self.assertEqual(coverage['华中']['snapshot'], 'v2')
        # 恢复后重送事件 4：正常应用，不重复不倒退
        result2 = run(state, make_config(), FakeClient(changes={
            'central|3|': segment('central', [ev(4, 'revoke', 'd-7')],
                                  snapshot='v2')}), clock=CLOCK)
        self.assertEqual(dealers_of(result2)['d-7']['status'], 'revoked')
        self.assertEqual(state['scopes']['central']['last_sequence'], 4)

    def test_change_cursor_invalid_recovers_via_rebaseline(self):
        state = seed_state()
        run(state, make_config(), FakeClient(changes={
            'central|0|': segment('central', [
                ev(1, 'transfer', 'd-1', from_region='华中', to_region='华南')])}),
            clock=CLOCK)
        client = FakeClient(
            directory={
                '华中|': page('central', 'v2', None, []),
                '华南|': page('central', 'v2', None, [dealer('d-1', '华南')]),
            },
            changes={'central|1|': CursorInvalid('游标失效'),
                     'central||': segment('central', [ev(5, 'revoke', 'd-1')],
                                          snapshot='v2')})
        result = run(state, make_config(), client, clock=CLOCK)
        scope = state['scopes']['central']
        self.assertIsNone(scope['needs_reconcile'])
        self.assertEqual(scope['last_sequence'], 5)
        self.assertTrue(any(s['from'] == 2 and s['to'] == 4
                            for s in state['sequence_skips']))
        self.assertEqual(dealers_of(result)['d-1']['status'], 'revoked')
        self.assertTrue(any('重基线' in w for w in result['warnings']))

    def test_snapshot_does_not_resurrect_terminal_status(self):
        state = seed_state()
        run(state, make_config(), FakeClient(changes={
            'central|0|': segment('central', [ev(1, 'revoke', 'd-1')])}),
            clock=CLOCK)
        result = run(state, make_config(), FakeClient(directory={
            '华中|': page('central', 'v2', None, [dealer('d-1', '华中')]),
            '华南|': page('central', 'v2', None, []),
        }), force_reconcile=True, clock=CLOCK)
        self.assertEqual(dealers_of(result)['d-1']['status'], 'revoked')
        self.assertTrue(any('终止状态' in w for w in result['warnings']))


class AuthorizationTest(unittest.TestCase):
    def test_authorization_shrink_marks_unconfirmable_not_deregistered(self):
        state = seed_state()
        result = run(state, make_config(authorized_regions=['华中']),
                     FakeClient(), clock=CLOCK)
        dealers = dealers_of(result)
        self.assertFalse(dealers['d-7']['confirmable'])
        self.assertEqual(dealers['d-7']['unconfirmable_reason'],
                         'region_not_authorized')
        self.assertEqual(dealers['d-7']['status'], 'active')  # 不是已注销
        self.assertEqual(dealers['d-7']['basis']['type'], 'snapshot')
        self.assertTrue(dealers['d-1']['confirmable'])

    def test_absent_from_complete_snapshot_is_unconfirmable_not_revoked(self):
        state = seed_state()
        result = run(state, make_config(), FakeClient(directory={
            '华中|': page('central', 'v2', None, [dealer('d-1', '华中')]),
            '华南|': page('central', 'v2', None, []),
        }), force_reconcile=True, clock=CLOCK)
        dealers = dealers_of(result)
        self.assertFalse(dealers['d-2']['confirmable'])
        self.assertEqual(dealers['d-2']['unconfirmable_reason'],
                         'absent_from_snapshot')
        self.assertEqual(dealers['d-2']['status'], 'active')  # 不推断注销
        self.assertEqual(dealers['d-2']['last_confirmed']['basis']['snapshot'], 'v1')

    def test_reauthorized_region_recovers_only_after_fresh_snapshot(self):
        state = seed_state()
        run(state, make_config(authorized_regions=['华中']), FakeClient(), clock=CLOCK)
        # 重新授权但尚未核对：仍然不可再确认
        result = run(state, make_config(), FakeClient(), clock=CLOCK)
        self.assertFalse(dealers_of(result)['d-7']['confirmable'])
        # 全量核对后以来源为依据恢复
        result = run(state, make_config(), FakeClient(directory={
            '华中|': page('central', 'v2', None, [dealer('d-1', '华中')]),
            '华南|': page('central', 'v2', None, [dealer('d-7', '华南')]),
        }), force_reconcile=True, clock=CLOCK)
        dealers = dealers_of(result)
        self.assertTrue(dealers['d-7']['confirmable'])
        self.assertEqual(dealers['d-7']['basis']['snapshot'], 'v2')


class DeliveryTest(unittest.TestCase):
    def test_delivery_states_coverage_versions_and_basis(self):
        state = empty_state()
        result = run(state, make_config(), FakeClient(directory={
            '华中|': page('central', 'v1', 'h2', [dealer('d-1', '华中')]),
            '华中|h2': page('central', 'v1', None, []),
        }), clock=CLOCK)
        self.assertEqual(result['uncovered_regions'], ['华南'])
        coverage = {c['region']: c for c in result['coverage']}
        self.assertEqual(coverage['华中']['snapshot'], 'v1')
        basis = dealers_of(result)['d-1']['basis']
        self.assertEqual(basis['type'], 'snapshot')
        receipt_ids = {r['receipt_id'] for r in state['receipts']}
        self.assertTrue(set(basis['receipt_ids']) <= receipt_ids)
        self.assertEqual(result['delivery_id'], 'run-000001')
        self.assertEqual(result['summary']['receipts_preserved'],
                         len(state['receipts']))

    def test_receipts_preserved_across_recovery(self):
        state = seed_state()
        run(state, make_config(), FakeClient(changes={
            'central|0|': segment('central', [ev(1, 'revoke', 'd-1')])}),
            clock=CLOCK)
        before = list(state['receipts'])
        # 缺段触发全量核对恢复
        run(state, make_config(), FakeClient(
            directory={
                '华中|': page('central', 'v2', None, [dealer('d-2', '华中')]),
                '华南|': page('central', 'v2', None, [dealer('d-7', '华南')]),
            },
            changes={'central|1|': segment('central', [ev(3, 'revoke', 'd-7')],
                                           snapshot='v2')}), clock=CLOCK)
        after_ids = {r['receipt_id'] for r in state['receipts']}
        for receipt in before:
            self.assertIn(receipt['receipt_id'], after_ids)


class ContractTest(unittest.TestCase):
    def test_directory_page_validation(self):
        with self.assertRaises(ContractError):
            parse_directory_page(json.dumps(
                {'snapshot': 'v1', 'dealers': []}).encode())  # 缺 scope_id
        with self.assertRaises(ContractError):
            parse_directory_page(json.dumps({
                'scope_id': 'c', 'snapshot': 'v1',
                'dealers': [dealer('d-1', '华中'), dealer('d-1', '华中')],
            }).encode())  # 页内重复
        with self.assertRaises(ContractError):
            parse_directory_page(json.dumps({
                'scope_id': 'c', 'snapshot': 'v1',
                'dealers': [{'dealer_id': '', 'region': '华中', 'status': 'x'}],
            }).encode())

    def test_change_segment_validation(self):
        with self.assertRaises(ContractError):
            parse_change_segment(json.dumps({
                'scope_id': 'c',
                'events': [ev(1, 'rename', 'd-1')]}).encode())  # 未知事件类型
        with self.assertRaises(ContractError):
            parse_change_segment(json.dumps({
                'scope_id': 'c',
                'events': [ev(1, 'transfer', 'd-1')]}).encode())  # 转移缺区域
        with self.assertRaises(ContractError):
            parse_change_segment(json.dumps({
                'scope_id': 'c',
                'events': [{'sequence': '1', 'kind': 'revoke',
                            'dealer_id': 'd-1'}]}).encode())  # 序列号类型错误

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            parse_config({'source_id': 's', 'authorized_regions': []})
        with self.assertRaises(ValueError):
            parse_config({'source_id': 's',
                          'authorized_regions': ['华中', '华中']})


class ServiceIntegrationTest(unittest.TestCase):
    def test_run_pipeline_with_demo_fixtures(self):
        config_path = ROOT / 'fixtures' / 'config.sample.json'
        fixture_path = ROOT / 'fixtures' / 'demo-source.json'
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / 'state.json'
            delivery_path = Path(tmp) / 'delivery.json'
            result1 = run_pipeline(config_path, state_path, delivery_path,
                                   fixture_path=fixture_path, clock=CLOCK)
            self.assertEqual(len(result1['coverage']), 2)
            self.assertEqual(result1['summary']['confirmable'], 5)
            result2 = run_pipeline(config_path, state_path, delivery_path,
                                   fixture_path=fixture_path, clock=CLOCK)
            dealers = dealers_of(result2)
            self.assertEqual(dealers['d-3']['region'], '华南')
            self.assertEqual(dealers['d-8']['status'], 'revoked')
            self.assertEqual(result2['summary']['events_applied'], 2)
            saved = json.loads(delivery_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['delivery_id'], result2['delivery_id'])


if __name__ == '__main__':
    unittest.main()
