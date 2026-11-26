# 项目进度

状态以本文件为准。开发约束见 `AGENTS.md`，怎么运行见 `README.md`。
不要另开设计/交接文档。新 session 先读本文件再动手。

## 大目标

做出**企业级智能问数**：用户用自然语言问业务数据，不必知道哪个系统、哪个库、哪张表。面对的是几十个系统、成百上千张表，不是手挑的演示 schema，也不是只能命中固定问句模板。

语义真源是 [alibaba/UnifiedModel](https://github.com/alibaba/UnifiedModel) 的模型包（实体、关系、认证指标、系统简介）。[NVIDIA Auto Ontology](https://github.com/NVIDIA/auto-ontology) 只负责从元数据提议同义词和候选关系，减轻人工对齐；不让它或任何模型写 SQL / SPL。本仓库编译有界 `QueryPlan`，扇出和未认证口径必须拒绝，角色不清必须追问。最终要有类型化 Agent 循环和企业控制（权限、审计、只读）。

这是对招聘域 `Hoiikk/umodel-agent-platform` 的干净重写：旧仓库字段和追问写在代码里，主管要求换成可治理的通用问数。产品形态是「认证对象上的有界问数」——不会就算清楚地澄清或拒绝，不是 BIRD 式随便生成 SQL。

## 项目目标

用自然语言问企业数据，系统自己完成这条链路：判断问题类型，从大量系统的元数据里找到相关库和表，用 **alibaba/UnifiedModel** 与 **NVIDIA Auto Ontology** 对齐实体和指标，把编号核到真实数据，歧义时追问而不是猜，再按认证口径做有界聚合。最终还要有真正的 Agent 循环和企业控制。只回答固定问句模板不算完成。

验收项（细则在 `AGENTS.md`）：

1. 区分数据问题、闲聊、外部工具、不安全写操作、不支持的请求，以及需要澄清的请求。
2. 从元数据发现业务系统、数据库和少量相关表。目标规模是几十个系统、几百到几千张表。
3. 用 UModel 和 Auto Ontology 对齐实体与指标，并评测检索质量。只有评测证明有缺口时才考虑 Wren/dbt。
4. 把 `SK-0001` 这类编号解析到目录声明的键，并用只读查询核对真实值。
5. 发现买方城市和卖方城市这类冲突。证据不足时返回澄清，不擅自选角色。
6. 聚合必须有明确指标、粒度、时间和币种语义，并检查连接基数，防止一行被乘成多行。
7. 真正的 Agent 流程：注册工具、有界规划与观察、上下文、结果后处理、终止检查、有界重试。
8. 生产前的企业控制：身份传递、行列权限、只读执行、审计、预算、取消、密钥和 schema 漂移。

## 现在到哪

Phase 1 到 Phase 6 已完成（Phase 6 已在 `main`）。问句会先做预检和三级检索，再槽位填充；能确定时生成一份有界 `QueryPlan` 并只读执行。角色或口径没定时保存会话，下一轮合并槽位后再校验。用户放弃则结束本轮，不查库。

**还没有：** Agent 循环、alibaba/UnifiedModel 接线、NVIDIA Auto Ontology、多引擎 connector、招聘库 `rms`、BIRD 评测。

语义真源目前是本地 `testdata/enterprise-data-agent-fixture/benchmark/semantic_model.json`，形状扮演 UModel 快照，**不是** UnifiedModel workspace。执行器只连合成夹具的 SQLite。

当前编译器边界：一条 SQL 仍是一张事实表加目录批准的安全维度；聚合只有 `SUM`、`COUNT`、`AVG`。Phase 5 额外允许：本体 `current_filter` 恢复一行后的版本形状、两个已认证聚合分别执行再相减、快照事实上的 `as_of_date` 等值过滤、带有效期映射表的二次只读对齐。不生成嵌套 SQL、自由 CTE、窗口函数或跨库 JOIN。槽位不含 SQL、连接或字段引用。未标成 `known` 的关系不能进编译器。

### 给下一个 session 的第一件事

评审通过后做 **Phase 7：UModel 适配**（把夹具拆成 YAML model pack，加载器对拍现有 JSON，规划器不改算法）。不要做 connector、不要导入 BIRD/`rms`、不要接 Auto Ontology 的 `run_sql`、不要做自由 SQL/SPL Agent。动手前等用户说「执行 / 继续」。

## 问句现在怎么走

```
用户问句
  → IntentRouter（config/default.json 规则：闲聊 / 写操作 / 天气 / 问数）
  → SemanticRetriever.retrieve（先 rank_systems，再系统内 search，交给预检 Top-8 表）
  → 编号精确查找（只读 SQLite，参数绑定）
  → AmbiguityDetector（买方/卖方、指标口径）
  → extract_slots → SlotProposal（禁止 sql/joins/field_id）
  → QueryPlanner 从本体生成 QueryPlan
  → QueryPlanValidator（扇出、known 关系、认证指标）
  → QueryExecutor 渲染 SELECT + 参数绑定（仅 SQLite）
```

代码入口：`src/enterprise_data_agent/`。配置：`config/default.json`。夹具：`testdata/enterprise-data-agent-fixture/`。大目录生成器：`testdata/large-catalog/generate.py`（测试时写临时目录，不入库）。

## 已完成

### Phase 1 — 目录发现与解析

- 读取并校验目录和语义模型。数据库路径不能逃出测试数据根目录。
- 规则路由：数据问题、闲聊、已配置的外部工具、写操作、暂不支持。词表和置信度在 `config/default.json`。
- 词法检索：表、字段、指标别名、粒度、中文字片段；打分权重可配置。
- 精确编号查找：只读 SQLite，参数绑定，并绑到编号模式声明的表和列。
- 歧义检测：角色或指标口径不清时返回澄清。角色词和提示语来自语义模型。
- 预检 JSON 仍是发现证据：`intent`、`selected_system`、`candidate_tables`、`resolved_metric`、`resolved_entities`、`clarification`。`selected_system` 只是最高分表所属系统；`resolved_metric` 只在唯一别名命中时出现。可执行计划由规划器另行生成。

### Phase 2 — 查询计划校验、执行与规划

- `queryplan.py`：`QueryPlan` 与 `QueryPlanValidator`。校验器按目录重算数据源、字段、指标定义、粒度、连接基数、角色过滤、币种、时间、聚合和行数上限，不相信计划自报的基数。
- 粒度按去停用词后的词序列相等比较。`contract` 对得上 `one row per contract`，对不上 `one row per contract item`。
- `query_mode`：`aggregate` 必须有指标；`point_lookup` 必须在主键、`*_id` 或本体编号列上有精确等值条件。
- 扇出保护：带 `fanout_warning` 的关系（如 `order_to_items`）和时间快照关系直接拒绝。一对多只有在本体要求的角色列上做单值等值过滤时才放行。
- 非法计划只返回结构化原因，不生成 SQL。原因包括 `unknown_model`、`unknown_field`、`metric_not_certified`、`unresolved_ambiguity`、`grain_mismatch`、`unsupported_aggregate`、`cross_source_plan`、`relationship_not_approved`、`fanout_risk`、`currency_policy_required`、`time_policy_required`、`query_budget_exceeded`、`empty_metric_set`、`unsupported_query_mode`、`point_lookup_requires_exact_filter`。
- `executor.py`：先复验，再渲染只含 `SELECT` 的 SQL。标识符来自白名单，字面量全部参数绑定。连接使用 `mode=ro` 和 `PRAGMA query_only=ON`。
- 策略值在 `config/default.json` 的 `query_plan` 下。
- `planner.py`：从预检结果生成一份计划。指标、维度、角色过滤、认证过滤和编号都来自语义模型或预检里已经核对过的值。时间词、币种说法、行数上限和时区在 `planner` 配置里。
- 歧义未解除时，计划标成 `unresolved`，校验器拒绝执行，不会查库。
- 有配置的时间词时，SQL 使用半开区间 `[start, end_exclusive)`，边界值参数绑定。
- 命令行会规划并在计划合法时执行。

### Phase 3 — 槽位填充与多轮澄清

- `slots.py`：`SlotProposal` 只接受指标、维度、时间词和币种。带 `sql`、`joins` 或 `field_id` 的提案直接拒绝。`SessionState` 保存未完成计划和澄清请求。
- 默认别名匹配是子序列，精确命中优先。`alias_match` 改成 `exact` 后，「合同总金额」重新拒绝，不改代码。
- 规划器从槽位组装计划，连接和字段仍由 `QueryPlanner` 从本体生成。`QueryPlanValidator` 与 `SqlRenderer` 未改。
- 问句里配置的未支持约束（同比、环比、窗口函数）若进不了当前计划，返回 `unmatched_constraint`，不查库。
- 澄清不能跳过。跟进句对不上选项时继续追问。放弃短语在 `slot_filling.abandon_phrases`。
- 注入的槽位仍会并入问句里扫到的未支持约束，不能靠注入绕过拒绝。
- 词法检索保持为预检基线。没有接入在线大模型；测试用词法提取或注入 `SlotProposal`。

### Phase 4 — 时间分桶、比较、IN 与 Top-N

- 分桶字段必须是本体 `time_role` 列，粒度来自 `planner.time_bucket_phrases` 或维度上的 `bucket_grain`。Q009 使用 `signed_at` 的月分桶，并按币种分组。
- 比较和 `IN` 只写到认证度量或已选定的单一角色维度。没有买方/卖方角色时，带城市值的问句仍澄清，不猜角色。
- Top-N 写成 `ORDER BY metric_0 LIMIT N`。Q040 在指标口径未定时不排序、不查库。超出 `query_plan.max_limit` 的 Top-N 被拒绝。
- 别名对不上时，子序列模式下可用配置的指标字片段重叠做唯一候选；阈值在 `slot_filling.min_metric_ngram_overlap`。`alias_match=exact` 时不走这条路径。
- 同比、环比、窗口函数仍是 `unmatched_constraint`。

### Phase 5 — 有证明的计划形状

- 版本：Q005 以 `contract_version` 为事实表，`is_current=1`，经 `version_to_header` 的多对一对到合同号。执行前按 `unique_key` 探测；同一键多行仍 `fanout_risk`。无 `current_filter` 的 `contract_to_version` 仍拒绝。
- 组合指标：`net_sales` 声明为两个已认证指标相减。Q016 分别对已付款订单和已批准退款做只读聚合，再在内存里按分组键相减，不把订单连到退款。
- 快照：Q021 在 `receivables.as_of_date` 上等值过滤。`invoice_to_receivable_snapshot` 一对多仍拒绝。
- 跨系统客户：Q006 在合同源上分组，再用 `customer_id_map` 的有效期窗口做第二次只读查询并对齐规范客户，不写跨库 JOIN。排序发生在映射之后。

### Phase 6 — 大目录检索

- 生成器 `testdata/large-catalog/generate.py` 在 4 个核心系统上再加 26 个干扰系统，合计 30 个系统、544 张表。含同名 `orders`/`contract_header`/`amount`/`city`，以及 `legacy_contracts.contract_header` 废弃表。大目录只写元数据，不造业务行，不用于执行。
- 三级检索：先按系统描述、域、表名和本体别名缩到 `top_systems`；再在系统内混合召回（词重叠、字片段、字段、指标别名、编号、废弃惩罚）；每系统最多 `per_system_tables` 张，全局交 `top_tables` 张给预检。
- 校验器拒绝 `status` 不是 `known` 的关系。`order_to_items` 仍 `fanout_risk`。Q014 可以执行，但不使用该连接。
- 大目录 9 条召回题的黄金表均出现在 Top-5（9/9）。改 `top_systems` 或 `deprecated_penalty` 会改变检索，不改代码。
- 40 题行为矩阵冻结当前决策（澄清/拒绝/执行/路由），不是 40/40 答对。金标为 clarify 但仍执行的题记在已知限制。

### 证据

- 2026-11-24：`/opt/anaconda3/bin/pytest -q` → **76 passed**（Phase 1 共 26 个，Phase 2 校验与执行 38 个，规划器 12 个）。
- 2026-11-26：`python -m pytest` → **91 passed**（Phase 1 共 26 个，Phase 2 校验与执行 38 个，规划器 13 个，Phase 3 共 14 个）。
- 2026-11-26：`python -m pytest` → **109 passed**（上项 91 个加上 Phase 4 共 18 个）。
- 2026-11-26：`python -m pytest` → **118 passed**（上项 109 个加上 Phase 5 共 9 个）。
- 2026-11-26：`python -m pytest` → **168 passed**（上项 118 个加上 Phase 6 共 50 个）。
- 规划器用真实 SQLite 执行了「按买方城市统计已签合同金额」、`SK-0001` 的含税金额，以及 Q001「去年签署的合同总金额是多少？」。香港角色不清时追问，回答「买方」后执行；Q040 追问口径且不查库；闲聊和「删掉合同 SK-0001」不查库。
- Phase 4 执行了 Q009 按 `signed_at` 月分桶并按币种分组、Q031 在买方角色下的城市 `IN`、Q033 认证度量上的 `>` 并保留币种分组。Q040 在选定口径前仍不排序、不查库。改 `time_bucket_phrases`、`comparison_patterns`、`top_n_patterns`、`value_connectors` 或 `min_metric_ngram_overlap` 会改变行为，不改代码。
- Phase 5 执行了 Q005 当前版本金额、Q016 净销售额两次只读相减、Q021 应收账款 `as_of_date` 等值过滤、Q006 经有效期映射后的最高客户。无 `current_filter` 的版本连接、订单行项目连接和应收快照一对多仍 `fanout_risk`。改 `planner.as_of_date` 会改变映射窗口，不改代码。

## UModel 与 Auto Ontology 怎么接（已定方案，尚未实施）

### 产品分别是什么

- **UModel** = [alibaba/UnifiedModel](https://github.com/alibaba/UnifiedModel)。对象图语义运行时：YAML/JSON **model pack**（`entity_set`、`entity_set_link`、`metric_set`、`Storage`），导入 workspace，用 SPL 读 `.umodel` / `.entity` / `.topo`。官方 Agent 路径是 MCP `query_spl_execute`。本仓库用它当**认证后的语义真源**，不用它当分析 SQL/SPL 引擎。
- **Auto Ontology** = [NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology)。从库表元数据编本体（术语、同义词、列映射、FK 候选）。官方答数路径是 `retrieve → run_sql → submit`（模型写 SQL）。本仓库只用 **Generate/Ground**：提案写入 UModel 的非 `known` 区。不采用 `ask_question` / `run_sql`。
- **本仓库** = 有界编译器 + 将来的类型化 Agent 循环。问句路径只读已经认证的对象，模型不能写 SQL，也不能写 SPL。

旧招聘仓库不是 UModel 产品；它只是对照基线。新系统必须「照用」alibaba/UnifiedModel，意思是业务对象活在 model pack 里，而不是再手写一份与 UModel 无关的 JSON 当永远真源。

### 两段时间线，不要混

```
【离线，加人】                                      【问句时，不加人】
catalog 表结构
  → Auto Ontology 提议同义词 / 关系 / 列描述         Catalog 已是内存 dict
  → 人审，写成 UModel YAML pack                      → 检索 / 槽位 / 编译 / SQLite
  → umctl import 进 workspace（可选；测试可直接读 pack）
  → 加载器填进 catalog.ontology（与今天 JSON 同形状）
```

问句进来时 **不**读 YAML、**不**调 Auto Ontology、**不**让模型拼 `.topo` Cypher。

「通用问答」在这套约束下 = 新库先变成模型包，同一套 Agent 能选系统、选 1～3 张表、澄清、在已证明形状上聚合；形状不够就结构化拒绝。不是 BIRD 金 SQL 全都能跑。若以金 SQL 为成功标准，会倒回 text-to-SQL，写死字段会换一种形式回来。

### YAML 好在哪

UModel 官方 pack 和 Auto Ontology 导出都是 YAML，适合人和 diff。问句路径继续用 Python dict。**不要**让 `planner.py` 去读一堆 YAML。

| 东西 | 格式 | 谁维护 |
|------|------|--------|
| UModel pack（真源） | YAML 目录 | 人或 Auto Ontology 提案 + 认证 |
| 测试夹具快照 | 现有 JSON 暂时保留 | 加载器从 pack 生成，对拍用 |
| 运行时 | dict | `Catalog.load` |
| 运行时策略 | `config/default.json` | 不改成 YAML |

### 当前三个 JSON，谁改谁留

| 文件 | 是否改 YAML |
|------|-------------|
| `testdata/enterprise-data-agent-fixture/benchmark/semantic_model.json` | **要拆成 pack。** 这是唯一的语义真源替身 |
| `testdata/enterprise-data-agent-fixture/benchmark/catalog.json` | **Phase 7 第一刀不动。** 物理表/列/库路径。系统简介以后可变成 UModel Storage / domain metadata |
| `config/default.json` | **不要改成 YAML。** 权重、阈值、路由句式 |

`questions.jsonl`、SQLite、`generate.py` 不转 YAML。`testdata/large-catalog/benchmark/` 是生成物，已 gitignore。

`semantic_model.json` 各段对应 pack：

| JSON 键 | YAML |
|---------|------|
| `canonical_entities` | `entity_set/*.yaml` |
| `relationships` | `link/*.yaml`（必须带 `cardinality`、`status`；UModel 原生没有这两字段，放 `spec` 扩展） |
| `metrics` | `metric/*.yaml`（aliases、model、measure、filters、composition、authority） |
| `dimensions` | 实体字段或 `dimension/*.yaml`（`role_filter`、`time_role`） |
| `ambiguity_cases` | 扩展 YAML（UModel 无现成 kind） |
| `identifier_patterns` | 写在 EntitySet 字段上或单独 patterns YAML |
| `security_fixture` | 先留 JSON / 以后的策略文件，不塞进业务 pack |

不要第一步就删 `semantic_model.json`。流程：YAML → 生成同等 JSON 或内存 dict → 旧测试仍过 → 再让 `Catalog.load` 直接读 pack。

### Phase 7 第一刀建议只拆这些

够证明「YAML → 现有 ontology 形状 → Q001 / 香港澄清 / SK-0001 不变」：

- EntitySet：`customer`、`contract`
- 关系：`contract_to_party`、`version_to_header`
- 指标/维度：`signed_contract_amount`、`buyer_city`、`seller_city`
- 编号：`SK-` 模式
- 歧义：香港买方/卖方那条

其余指标、财务、净销售仍可读现 JSON，加载器合并。目录示例：

```text
testdata/enterprise-data-agent-fixture/umodel/
  contracts/entity_set/contract.yaml
  contracts/link/contract_to_party.yaml
  contracts/metric/signed_contract_amount.yaml
  crm/entity_set/customer.yaml
```

关系示例必须能被今天的校验器消费（`status: known` 才能 JOIN；`candidate_*` 只能给检索加分）：

```yaml
kind: entity_set_link
metadata:
  name: contract_to_party
  domain: contracts
spec:
  src: { domain: contracts, kind: entity_set, name: contract }
  dest: { domain: contracts, kind: entity_set, name: contract_party }
  cardinality: one_to_many
  status: known
  role_filter_required: true
```

认证指标仍是声明，不是 SQL 模板：`model` + `measure` + `aggregation` + `filters` + `grain`。

### Auto Ontology（Phase 8，不要提前做）

对已接的库读 INFORMATION_SCHEMA + 只读抽样 → 术语/同义词/FK 候选 → 转成上述 YAML 且全部 `status: candidate` → 人审升级 `known`。买方/卖方这种冲突生成 `ambiguity_cases`，不静默选。评测：同一大目录，开/关候选别名，对比 Top-5。

禁止：Chat/`ask_question` 当生产答数；把 SQL attributes 当可执行指标；提案自动变 `known`（否则 `order_to_items` 会穿扇出门）。

### 主管 5 个问题接到哪

1. **是不是问数**：现有规则预路由保留。`needs_classification` 以后才给模型。业务词（合同、订单、客户）应从 UModel EntitySet 来，不应长期留在 `routing.data_signals`。
2. **哪个系统/库/表**：UModel domain + Storage 简介给 Agent 选库。检索继续三级，输入改为 pack 导出的同一形状。禁止模型自由写 SPL。
3. **库内 1～3 张表 + 聚合**：找表用 Auto Ontology 别名提案 + 词法；聚合仍走编译器。人工从「写同义词」变成「认证提案」。
4. **SK-xxx**：认证编号模式 + 选中源上参数绑定精确查找。UModel `.entity` 模糊检索不能当答案。
5. **香港买方还是卖方**：强制澄清 + `SessionState`。Auto Ontology 只负责发现冲突并生成 ambiguity 提案。

### 将来的类型化 Agent（Phase 9，现在不要做）

`QueryPlanner.plan()` 仍是一条直线。循环补上之后，终局工具是 `compile_plan` / `execute_readonly`，不是 SQL。工具集合固定：路由、读 UModel 元数据（仅模板化 `.umodel`）、检索、槽位、编号、澄清、编译、执行。禁止运行时注册 `run_sql`。有界步数；`fanout_risk` 不重试换 JOIN。

### Connector、rms、BIRD（明确延后）

- **connector**：现在不要加。只有 SQLite 时抽接口是空抽象。第二个真实引擎出现再从执行器拆。
- **rms / BIRD**：Agent 稳定后再做「不改推理、只加模型包」的评测。BIRD 测选库/选表/歧义，不以金 SQL 匹配为通过条件。覆盖不了的题记 `refuse`，算覆盖率，不当 Agent 失败。

## 未完成

### Phase 2 / 3 / 4 / 5 明确不做

这些限制保持到后面点名的阶段。

- 链式连接、预聚合节点、事实表连事实表、自由 CTE。订单行项目等带 `fanout_warning` 的关系仍拒绝。
- 度量不是物理列、认证过滤写不成声明过滤、或维度没有已批准映射时，拒绝而不改写 SQL。
- Q037 的订单编号模式没有绑到表，本阶段不靠猜测补表名。

### 已知限制

- `unsupported_query_mode` 已在校验器中返回，测试没有单独断言这个原因。
- `test_executor_connects_read_only` 只检查 SQL 以 `SELECT` 开头，没有对只读连接执行写语句。
- 40 题已整套跑过决策矩阵；矩阵记录的是当前行为，不是与 `expected_behavior` 逐条对齐。Q007、Q013、Q034、Q039 金标为澄清，当前会执行。Q014 执行时不连 `order_items`，因此没有明细。Q031 仍把「华南」当城市。
- 合成夹具里 2026 年没有 `SIGNED` 合同，因此 Q009「今年每月」计划合法、SQL 走 `signed_at` 月分桶，结果行数可以为 0。
- Q031 把「华南」当作买方城市值过滤；夹具里华南是客户区域，不是 `contract_party.city`，该值不会命中行。
- 没有接入 alibaba/UnifiedModel。`Catalog.load` 只读 JSON。
- `routing.data_signals` 仍含「合同」「订单」「客户」等业务词。新域没有这些词时，问数可能落到 `needs_classification`。
- 执行器只支持 SQLite 只读。多引擎 connector 延后。
- 没有在线大模型槽位填充。默认提取是词法子序列；大模型只能注入 `SlotProposal`，不能写 SQL。
- 检索仍是词法规则，还没有和 Auto Ontology 比过召回。
- 实体只有精确匹配。模糊匹配不能直接当答案。
- 没有生产认证、审计或权限。
- 没有工具循环。不能把规划器叫做 Agent。

## 决策

- 基线是招聘领域的 `Hoiikk/umodel-agent-platform`。它的字段和追问规则写在代码里，不适合大目录发现和受治理的分析，因此在本目录干净重写，不改原仓库。
- 语义真源是 alibaba/UnifiedModel 的 workspace 模型包。当前用 `semantic_model.json` 作为同形状快照，好让编译器先可测。Phase 7 用 YAML pack + 加载器对拍，不改规划器算法。
- NVIDIA Auto Ontology 只用于从目录元数据生成术语、同义词和候选关系，写入 UModel 的非 `known` 区。不采用它的 `run_sql` / `ask_question`，也不让模型写 SPL。认证指标、角色过滤、扇出状态须显式升级为 `known`。
- `rms` 与 BIRD 模型包是后续评测，用来证明「不改 Agent 推理、只加包」。不是当前开发门禁。
- 多数据库 connector 延后。有第二个引擎需求时再从 SQLite 执行器抽出协议。
- 可变的业务词、权重和阈值放配置或语义模型（将来放 UModel pack）；代码只保留算法和不变量。
- 模型提议槽位，规划器生成计划。校验器能拦住不合法的计划，拦不住合法但选错口径的计划。选错口径靠槽位约束和澄清拦住。
- 排序分是证据，不是置信度。候选关系在标成 `known` 之前不能用于连接。
- 澄清不能跳过并按猜测执行。用户放弃时结束本轮，不查库。多轮里的改写只补全指代；改写带入的新约束必须进入槽位，否则拒绝执行。
- 检索按表列、指标、业务规则分开召回，剪枝结果必须是目录中的标识。历史问句和标准问答只说明用过哪些表和指标，不执行其中的 SQL。业务规则写进语义模型或配置，由规划器变成过滤条件。方言函数检索不引入，SQL 仍由渲染器生成。Wren 只作节点划分的参考，不接入它的 SQL 生成。
- 大目录先按业务域缩小范围，再在域内召回表列元数据、可执行的业务定义，以及历史问句用过的表和指标。一张表有一个归属域，用于路由和责任划分。域过滤不能代替已批准的跨系统关系，也不能代替执行时的行列权限。黄金问答里的 SQL 不作为 few-shot，也不是答对新问题的条件。不部署 Vanna 或 DataHub。
- 指标按原子度量、简单指标、组合指标分层声明。组合指标的表达式只能引用已声明的指标名，过滤保持字段、运算符和参数绑定，不使用 SQL 模板。调用方只提交指标名、维度名、时间粒度、过滤、排序和行数上限，名称必须存在于语义模型。Phase 5 的第一刀只做同一粒度上的两个已认证指标相减。比率、去重计数和时间偏移等粒度对齐有测试后再加。编译器内部生成的子查询不等于开放自由 CTE。MetricFlow 只作声明和查询字段的参考，不接入 MetricFlow 或 dbt。

## 下一步

Phase 6 已完成。评审通过后按下面顺序做；未点名的阶段不要提前开工。

1. **Phase 7 — UModel 适配（下一刀）**  
   夹具写成 UnifiedModel YAML pack（先合同/客户最小集）；转换/加载成今天的 ontology dict；与手写 JSON 对拍，Q001、香港澄清、SK-0001 行为不变。不要接 MCP 分析路径，不要让模型写 SPL。`semantic_model.json` 先留着当对照。改 `routing.data_signals` 里的业务词属于本阶段或紧随其后，不要继续把「合同」「订单」当问数开关。

2. **Phase 8 — Auto Ontology 提案**  
   对同一目录做术语/别名/候选边；召回与纯词法对比；提案不得自动变 `known`。

3. **Phase 9 — 类型化 Agent 循环**  
   工具注册、有界观察、SessionState、后处理、终止与重试。终局是编译器，不是 SQL。

4. **再后**  
   企业控制（行列策略、身份、审计、预算）才能称生产。RMS/BIRD 共存评测、多引擎 connector 在 Agent 稳定后做。覆盖面仍靠有证明的计划形状逐个加；校验器继续拒绝扇出和未认证口径。只有评测证明某条口径无法被现有语义模型声明时，才考虑 Wren/dbt。
