# Enterprise Data Agent

干净重写的企业数据助手。当前进度、目标和未完成项见 [`progress.md`](progress.md)。开发约束见 [`AGENTS.md`](AGENTS.md)。

命令行会做发现预检，并在口径明确且形状合法时生成查询计划、只读执行。歧义、闲聊和写操作不会查库。

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

76 个测试使用 `testdata/enterprise-data-agent-fixture/` 里的本地 SQLite。其中 26 个覆盖目录、路由、检索、编号和歧义；38 个覆盖查询计划的接受、拒绝和只读执行；12 个覆盖从问句到计划的接受和拒绝。
