# 经销商名册的授权范围与变更领取

采集分三种方式，由 app/pipeline.py 编排：

- **按区域授权领取**：授权区域从本地配置给出，每个区域独立分页领取。
  分页游标绑定来源版本（snapshot）与区域；只有游标走完的完整领取才
  提交，空页不等于全部退出，半成品不会覆盖旧记录。
- **增量变更**：变更流事件（transfer/merge/revoke）按 scope 内递增的
  sequence 应用。经销商身份只认 dealer_id，绝不按名称合并。重送的
  事件去重，缺段或游标失效则转入全量核对恢复。
- **定期全量核对**：按 reconcile_interval_runs 周期或 --full 强制。
  授权缩小时，旧记录标为不可再确认（保留最后确认依据），而不是推断
  已注销；缺段序列区间由全量核对覆盖后记入 sequence_skips，先前收据
  全部保留。

每次交付说明覆盖了哪些区域、哪个来源版本、哪些经销商状态确有来源
依据（快照或事件，指向保留的收据）。状态跨运行持久化，重启后重送的
事件不会造成重复或倒退。

来源与字段约定见 docs/source-contract.md。

运行既有单响应示例：

    python -m app.service fixtures/sample.json

运行采集（离线 fixtures 回放演示，首次全量核对，再次增量变更）：

    python -m app.service run --config fixtures/config.sample.json \
        --state /tmp/state.json --delivery /tmp/delivery.json \
        --fixtures fixtures/demo-source.json

去掉 --fixtures 则按配置里的 URL 模板访问真实来源；--full 强制本次
全量核对。

运行测试：

    python -m unittest discover -s tests -v

检查编译：

    python -m compileall -q app tests
