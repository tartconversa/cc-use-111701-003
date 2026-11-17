"""交付说明：每次交付覆盖哪些区域、哪个来源版本、
哪些经销商状态确有来源依据。

交付只陈述有来源依据的事实：
- coverage 列出本次状态所依据的各区域完整核对（来源版本、页数、收据）；
- uncovered_regions 列出已授权但从未完整覆盖的区域；
- 每个经销商条目携带 basis（快照或事件），指向保留的收据；
- confirmable=False 的条目是不可再确认，而不是已注销。
"""


def build(state, config, notes, clock):
    dealers = []
    for dealer_id in sorted(state['dealers']):
        record = state['dealers'][dealer_id]
        dealers.append({
            'dealer_id': dealer_id,
            'region': record['region'],
            'status': record['status'],
            'confirmable': record['confirmable'],
            'unconfirmable_reason': record['unconfirmable_reason'],
            'merged_into': record.get('merged_into'),
            'basis': record['basis'],
            'last_confirmed': record.get('last_confirmed'),
        })
    coverage = []
    for region in config.authorized_regions:
        cov = state['coverage'].get(region)
        if cov is not None:
            coverage.append({'region': region, **cov})
    uncovered = [r for r in config.authorized_regions if r not in state['coverage']]
    return {
        'delivery_id': 'run-%06d' % state['run_id'],
        'source_id': config.source_id,
        'generated_at': clock(),
        'authorized_regions': list(config.authorized_regions),
        'coverage': coverage,
        'uncovered_regions': uncovered,
        'dealers': dealers,
        'summary': {
            'dealers_total': len(dealers),
            'confirmable': sum(1 for d in dealers if d['confirmable']),
            'unconfirmable': sum(1 for d in dealers if not d['confirmable']),
            'events_applied': notes.events_applied,
            'events_deduplicated': notes.events_deduplicated,
            'receipts_preserved': len(state['receipts']),
        },
        'warnings': list(notes.warnings),
    }
