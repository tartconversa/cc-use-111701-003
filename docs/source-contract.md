# 来源契约

目录 JSON 含 scope_id、snapshot、next_cursor、dealers[{dealer_id,region,status}]。变更流另有 sequence、kind、dealer_id、from_region、to_region；sequence 在 scope 内递增，事件可以重送。授权区域从本地配置给出。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 dealer_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

## 采集语义（app/pipeline.py）

**分页与游标绑定。** 目录按授权区域逐区域领取（contract.DirectoryPage）。
游标绑定来源版本与区域（contract.CursorBinding）：一次领取中途
scope_id 或 snapshot 变化即视为游标失效，丢弃暂存并重新全量领取；
HTTP 409/410 同样按游标失效处理。只有 next_cursor 走完的完整领取才
提交——空页只是空页，不是全部退出；未完成的领取不提交任何缺席结论。
同一来源版本内 dealer_id 重复出现属于来源错误，该次领取作废。

**增量事件。** 事件 kind 为 transfer（from_region→to_region）、merge
（target_dealer_id 为存续方）、revoke。经销商身份只认 dealer_id，
名称相同不合并。sequence 在 scope 内单调递增：已应用的序列重送去重，
收到的序列大于下一个期望序列即缺段，该事件及其后事件暂不应用。

**恢复。** 缺段、变更流游标失效或契约错误都会标记作用域待恢复，随后
对所有授权区域做全量核对；核对完成后缺段区间记入 sequence_skips 并
推进序列，变更流游标失效则从来源保留起点重基线。先前收据只增不减。

**可确认性。** 快照完整领取中缺席、或所在区域不再授权的经销商标为
不可再确认（confirmable=false，保留 last_confirmed 依据），绝不推断
为已注销；重新授权不会自动恢复，要等来源再次确认。merged/revoked 是
来源事件给出的终止状态，快照重新出现不会据此恢复，等待来源事件。
同一运行内新于快照领取的事件依据优先保留。来源版本（vN）倒退的核对
不采用，避免状态倒退。

**交付。** 每次交付（app/delivery.py）说明覆盖的区域与来源版本、未
覆盖的授权区域、每个经销商状态及其来源依据（快照或事件，指向保留的
收据 receipt_id）、应用与去重的事件数、以及全部警告。

来源配置与真实凭据由运行时提供，样例为合成数据。
