# 企业问数 Agent 合成测试数据 v1

这是为后续 Data Agent 开发准备的**合成数据**，当前不包含真实企业数据或真实个人信息。它用于先验证“找系统/表/列、实体值匹配、歧义澄清、查询计划、跨表聚合”等能力；Agent 开发尚未开始。

## 数据规模

- 4 个模拟业务系统：CRM、合同、销售、财务。
- 24 张表，每张表有 6–90 条左右的合成记录；数量刻意保持轻量，便于本机调试。
- 40 条带预期系统、必要表、指标、粒度、预期行为和评估提示的问题。
- 包含跨系统客户 ID 映射、合同买方/卖方同值城市、合同版本、订单头/订单行 fan-out、发票/付款分摊、AR 快照、不同金额口径和多币种。

本版本重点是**困难结构与问法覆盖**，不模拟企业规模。之后可用相同模式扩展到几十个系统/数百张表，或增加更多行数。

## 文件

- `generate_data.py`：使用 Python 标准库和固定数据规则重建全部文件。
- `data/crm.sqlite`、`data/contracts.sqlite`、`data/sales.sqlite`、`data/finance.sqlite`：4 个互相独立的 SQLite 数据源。
- `benchmark/catalog.json`：目录样例，含系统说明、表用途、粒度、字段名/类型/主键提示。
- `benchmark/semantic_model.json`：跨系统 canonical entity、关系候选、指标定义、别名、字段角色和显式歧义。
- `benchmark/questions.jsonl`：40 个问题样例及 expected behavior。`clarify`/`deny`/`route` 案例是预期正确结果，不是缺少 gold answer。

## 业务系统和关键难例

### CRM
客户主数据、账户、联系人、线索、跨系统 ID 映射、客户活动。`C001` 等是 CRM canonical customer ID；合同系统使用 `PTY-xxx`，财务使用 `FIN-xxx`。`customer_id_map` 有 validity interval，另有一个过期合同侧别名 `PTY-001-OLD`。

### 合同
合同主表、合同版本、合同参与方、合同条款、审批和状态历史。合同主表 `contract_header` 是签约金额的权威来源；同一合同在 `contract_party` 有 BUYER/SELLER 两行，城市值会重叠。`contract_version` 是版本粒度，不能和头表金额直接 join 后求和。

### 销售
订单头、订单行、报价、报价行、退款、销售代表。订单头一行一订单，订单行一行一商品。`orders.order_total` join 多条 `order_items` 后会重复；报价金额也不能误当作签约合同金额。

### 财务
发票、收款、付款分摊桥表、应收快照、汇率和财务调整。付款可能对应多个发票；应使用 `payment_allocations.allocated_amount` 做归属。多币种指标不允许裸加总，换算前要明确汇率日期口径。

## 生成和查看

```sh
python3 generate_data.py
```

示例查看表结构：

```sh
sqlite3 data/contracts.sqlite '.schema contract_header'
sqlite3 data/sales.sqlite 'select * from orders limit 5;'
```

问题集 JSONL 一行一个 JSON object，可由后续 benchmark runner 读取。数据生成是确定性的；重复运行会重建 `.sqlite` 文件。

## 使用注意

- 合成日期范围大致为 2024–2026；问题中“去年/今年”请在评测时固定 evaluation date，否则这类相对日期问题应使用明确时间范围。
- 金额字段有 HKD/CNY/USD 混合；没有定义通用换算时，必须按币种分组或先澄清汇率政策。
- 跨系统映射问题的 gold plan 描述的是多数据源查询步骤，不是可以直接对单个 SQLite 文件执行的 SQL。
- catalog/semantic model 是测试材料，Agent 只能依据其中的可见证据和审批状态做判断；不能把数据库字段名相似当成业务关系。
