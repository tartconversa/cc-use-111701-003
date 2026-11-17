# 来源契约

目录 JSON 含 scope_id、snapshot、next_cursor、dealers[{dealer_id,region,status}]。变更流另有 sequence、kind、dealer_id、from_region、to_region；sequence 在 scope 内递增，事件可以重送。授权区域从本地配置给出。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 dealer_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
