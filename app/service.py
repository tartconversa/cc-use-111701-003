"""既有单响应采集入口，以及按区域授权领取的运行入口。"""
import argparse
import json
import sys
from pathlib import Path
from .source import from_packet,fetch
from .extract import extract
from .ledger import receipt
from . import ledger
from . import pipeline
from . import state as state_mod
from .client import FixtureClient, UrlClient
from .config import load_config

KEY='dealer_id'

def inspect(packet):
    response=from_packet(packet)
    return receipt(response,extract(response,KEY))

def collect_one(url,source_id):
    response=fetch(url,source_id)
    return receipt(response,extract(response,KEY))

def run_pipeline(config_path, state_path, delivery_path=None, *,
                 client=None, fixture_path=None, force_reconcile=False, clock=None):
    """按区域授权领取、增量变更与全量核对的一个运行。

    状态在运行结束后持久化；交付说明可通过 delivery_path 落盘。
    """
    config = load_config(config_path)
    state = state_mod.load(state_path)
    if client is None:
        if fixture_path:
            client = FixtureClient(fixture_path, source_id=config.source_id)
        else:
            client = UrlClient(config)
    result = pipeline.run(state, config, client,
                          force_reconcile=force_reconcile, clock=clock)
    state_mod.save(state_path, state)
    if delivery_path:
        ledger.save(delivery_path, result)
    return result

def _run_main(args):
    parser = argparse.ArgumentParser(
        prog='app.service run',
        description='按区域授权领取、增量变更与定期全量核对')
    parser.add_argument('--config', required=True, help='本地授权与采集配置')
    parser.add_argument('--state', required=True, help='跨运行状态文件')
    parser.add_argument('--delivery', help='交付说明输出路径')
    parser.add_argument('--fixtures', help='离线回放来源响应的 JSON 文件')
    parser.add_argument('--full', action='store_true', help='强制本次全量核对')
    ns = parser.parse_args(args)
    result = run_pipeline(ns.config, ns.state, ns.delivery,
                          fixture_path=ns.fixtures, force_reconcile=ns.full)
    print(json.dumps(result, ensure_ascii=False, indent=2))

def main():
    args = sys.argv[1:]
    if args and args[0] == 'run':
        _run_main(args[1:])
        return
    if len(args)!=1: raise SystemExit('请提供来源响应文件')
    print(json.dumps(inspect(json.loads(Path(args[0]).read_text(encoding='utf-8'))),ensure_ascii=False,indent=2))

if __name__=='__main__': main()
