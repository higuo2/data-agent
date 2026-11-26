# Enterprise Data Agent

干净重写的企业数据助手。当前进度、目标和未完成项见 [`progress.md`](progress.md)。开发约束见 [`AGENTS.md`](AGENTS.md)。

命令行会做发现预检和槽位填充，并在口径明确且形状合法时生成查询计划、只读执行。歧义、闲聊和写操作不会查库。别名变体如「合同总金额」可以规划；角色不清时返回澄清。按月分桶、比较、`IN`、Top-N、当前版本、组合指标和带有效期的客户映射已接入。

## 运行预检

在本目录、Python 3.10+：

```sh
PYTHONPATH=src python -m enterprise_data_agent \
  --catalog testdata/enterprise-data-agent-fixture/benchmark/catalog.json \
  --ontology testdata/enterprise-data-agent-fixture/benchmark/semantic_model.json \
  --question "SK-0001 的含税合同金额是多少？"
```

换成 `香港地区的已签合同金额是多少？` 看澄清拒绝，换成 `你好` 看路由。输出里的 `preflight` 仍有候选表、指标和实体；`plan` 是查询计划，`execution` 只在计划通过校验后出现。

## 运行测试

```sh
python -m pip install -e ".[dev]"
pytest
```

118 个测试使用 `testdata/enterprise-data-agent-fixture/` 里的本地 SQLite。其中 26 个覆盖目录、路由、检索、编号和歧义；38 个覆盖查询计划的接受、拒绝和只读执行；13 个覆盖从问句到计划的接受和拒绝；14 个覆盖槽位填充和多轮澄清；18 个覆盖时间分桶、比较、`IN` 和 Top-N；9 个覆盖版本过滤、组合指标、快照日期和跨系统映射。
