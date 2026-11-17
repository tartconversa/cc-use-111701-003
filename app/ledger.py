"""将当前响应条目连同原件身份交付给调用者。"""
import json
from pathlib import Path

def receipt(response, items):
    return {'source_id':response.source_id,'url':response.url,
            'response_sha256':response.sha256,'items':items}

def page_receipt(response, *, scope_id, snapshot, region, page_index, item_count):
    """目录分页收据：绑定来源版本与区域，可追溯到原始响应摘要。"""
    receipt_id = 'page:%s:%s:%s:%d:%s' % (
        scope_id, region, snapshot, page_index, response.sha256[:16])
    return {'receipt_id': receipt_id, 'type': 'directory_page',
            'source_id': response.source_id, 'url': response.url,
            'response_sha256': response.sha256,
            'scope_id': scope_id, 'snapshot': snapshot, 'region': region,
            'page_index': page_index, 'item_count': item_count}

def segment_receipt(response, *, scope_id, snapshot, first_sequence, last_sequence, event_count):
    """变更流段收据：记录覆盖的序列区间。"""
    receipt_id = 'segment:%s:%s:%s:%s:%s' % (
        scope_id, snapshot or '-', first_sequence, last_sequence, response.sha256[:16])
    return {'receipt_id': receipt_id, 'type': 'change_segment',
            'source_id': response.source_id, 'url': response.url,
            'response_sha256': response.sha256,
            'scope_id': scope_id, 'snapshot': snapshot,
            'first_sequence': first_sequence, 'last_sequence': last_sequence,
            'event_count': event_count}

def save(path, result):
    Path(path).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
