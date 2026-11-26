# 项目进度

状态以本文件为准。开发约束见 `AGENTS.md`，怎么运行见 `README.md`。

## 项目目标

用自然语言问企业数据，系统自己完成这条链路：判断问题类型，从大量系统的元数据里找到相关库和表，用 UModel 与 NVIDIA Auto Ontology 对齐实体和指标，把编号核到真实数据，歧义时追问而不是猜，再按认证口径做有界聚合。最终还要有真正的 Agent 循环和企业控制。只回答固定问句模板不算完成。

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

Phase 1 到 Phase 5 已完成。问句会先做预检和槽位填充，能确定时生成一份有界 `QueryPlan` 并只读执行。角色或口径没定时保存会话，下一轮合并槽位后再校验。用户放弃则结束本轮，不查库。还没有 Agent 循环。

当前边界：一条 SQL 仍是一张事实表加目录批准的安全维度；聚合只有 `SUM`、`COUNT`、`AVG`。Phase 5 额外允许：本体 `current_filter` 恢复一行后的版本形状、两个已认证聚合分别执行再相减、快照事实上的 `as_of_date` 等值过滤、带有效期映射表的二次只读对齐。不生成嵌套 SQL、自由 CTE、窗口函数或跨库 JOIN。槽位不含 SQL、连接或字段引用。

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

### 证据

- 2026-11-24：`/opt/anaconda3/bin/pytest -q` → **76 passed**（Phase 1 共 26 个，Phase 2 校验与执行 38 个，规划器 12 个）。
- 2026-11-26：`python -m pytest` → **91 passed**（Phase 1 共 26 个，Phase 2 校验与执行 38 个，规划器 13 个，Phase 3 共 14 个）。
- 2026-11-26：`python -m pytest` → **109 passed**（上项 91 个加上 Phase 4 共 18 个）。
- 2026-11-26：`python -m pytest` → **118 passed**（上项 109 个加上 Phase 5 共 9 个）。
- 规划器用真实 SQLite 执行了「按买方城市统计已签合同金额」、`SK-0001` 的含税金额，以及 Q001「去年签署的合同总金额是多少？」。香港角色不清时追问，回答「买方」后执行；Q040 追问口径且不查库；闲聊和「删掉合同 SK-0001」不查库。
- Phase 4 执行了 Q009 按 `signed_at` 月分桶并按币种分组、Q031 在买方角色下的城市 `IN`、Q033 认证度量上的 `>` 并保留币种分组。Q040 在选定口径前仍不排序、不查库。改 `time_bucket_phrases`、`comparison_patterns`、`top_n_patterns`、`value_connectors` 或 `min_metric_ngram_overlap` 会改变行为，不改代码。
- Phase 5 执行了 Q005 当前版本金额、Q016 净销售额两次只读相减、Q021 应收账款 `as_of_date` 等值过滤、Q006 经有效期映射后的最高客户。无 `current_filter` 的版本连接、订单行项目连接和应收快照一对多仍 `fanout_risk`。改 `planner.as_of_date` 会改变映射窗口，不改代码。


## 未完成

### Phase 2 / 3 / 4 / 5 明确不做

这些限制保持到后面点名的阶段。

- 链式连接、预聚合节点、事实表连事实表、自由 CTE。订单行项目等带 `fanout_warning` 的关系仍拒绝。
- 度量不是物理列、认证过滤写不成声明过滤、或维度没有已批准映射时，拒绝而不改写 SQL。
- Q037 的订单编号模式没有绑到表，本阶段不靠猜测补表名。

### 已知限制

- `unsupported_query_mode` 已在校验器中返回，测试没有单独断言这个原因。
- `test_executor_connects_read_only` 只检查 SQL 以 `SELECT` 开头，没有对只读连接执行写语句。
- `benchmark/questions.jsonl` 的 40 题还没有整套跑过，没有覆盖率数字。
- 合成夹具里 2026 年没有 `SIGNED` 合同，因此 Q009「今年每月」计划合法、SQL 走 `signed_at` 月分桶，结果行数可以为 0。
- Q031 把「华南」当作买方城市值过滤；夹具里华南是客户区域，不是 `contract_party.city`，该值不会命中行。

### Phase 6 及以后

覆盖面靠后面逐个加的算子变大。校验器仍负责拒绝扇出和未认证口径。

- Phase 6：大目录检索。用同一生成方式扩到约 30 个系统、至少 500 张表，放入同名列和废弃表。然后做三级检索：先缩到少数系统，再在系统内混合召回，只把少数表交给槽位填充。UModel / Auto Ontology 只产生候选；没有标成 `known` 的关系不能进编译器。门禁是大目录上的 Top-5 表召回，以及扇出零逃逸。召回百分比等测完再写入，不预写目标。扇出测试从 Phase 5 放开新的连接形状时就开始。只有评测证明某条口径无法被现有语义模型声明时，才考虑 Wren/dbt。
- Phase 7：企业控制，通过后再做 Agent 循环。列白名单和行级策略要能拒绝越权问题。行级条件来自策略数据，由校验器检查后参数绑定，不在渲染器里写死列名。`security_fixture` 目前只有主体、列白名单和一句 `assigned sales region`，还没有可执行谓词。身份传递、审计、预算、取消、密钥和 schema 漂移也在本阶段。召回门禁和越权拒绝都有测试之后，再做工具注册、有界规划与观察、结果后处理、终止检查和有界重试。

### 现在还不能声称的能力

- 没有在线大模型槽位填充。默认提取是词法子序列；大模型只能注入 `SlotProposal`，不能写 SQL。
- 检索仍是词法规则，还没有和 Auto Ontology 比过召回。
- 实体只有精确匹配。模糊匹配不能直接当答案。
- 没有生产认证、审计或权限。
- 没有接入真实的 UModel / Auto Ontology 网络服务。
- 没有工具循环。不能把规划器叫做 Agent。

## 决策

- 基线是招聘领域的 `Hoiikk/umodel-agent-platform`。它的字段和追问规则写在代码里，不适合大目录发现和受治理的分析，因此在本目录干净重写，不改原仓库。
- 语义基础先用 UModel + NVIDIA Auto Ontology。开发数据用 `testdata/enterprise-data-agent-fixture/`。
- 可变的业务词、权重和阈值放配置或语义模型；代码只保留算法和不变量。
- 模型提议槽位，规划器生成计划。校验器能拦住不合法的计划，拦不住合法但选错口径的计划。选错口径靠槽位约束和澄清拦住。
- 排序分是证据，不是置信度。候选关系在标成 `known` 之前不能用于连接。
- 澄清不能跳过并按猜测执行。用户放弃时结束本轮，不查库。多轮里的改写只补全指代；改写带入的新约束必须进入槽位，否则拒绝执行。
- 检索按表列、指标、业务规则分开召回，剪枝结果必须是目录中的标识。历史问句和标准问答只说明用过哪些表和指标，不执行其中的 SQL。业务规则写进语义模型或配置，由规划器变成过滤条件。方言函数检索不引入，SQL 仍由渲染器生成。Wren 只作节点划分的参考，不接入它的 SQL 生成。
- 大目录先按业务域缩小范围，再在域内召回表列元数据、可执行的业务定义，以及历史问句用过的表和指标。一张表有一个归属域，用于路由和责任划分。域过滤不能代替已批准的跨系统关系，也不能代替执行时的行列权限。黄金问答里的 SQL 不作为 few-shot，也不是答对新问题的条件。不部署 Vanna 或 DataHub。
- 指标按原子度量、简单指标、组合指标分层声明。组合指标的表达式只能引用已声明的指标名，过滤保持字段、运算符和参数绑定，不使用 SQL 模板。调用方只提交指标名、维度名、时间粒度、过滤、排序和行数上限，名称必须存在于语义模型。Phase 5 的第一刀只做同一粒度上的两个已认证指标相减。比率、去重计数和时间偏移等粒度对齐有测试后再加。编译器内部生成的子查询不等于开放自由 CTE。MetricFlow 只作声明和查询字段的参考，不接入 MetricFlow 或 dbt。

## 下一步

Phase 5 已完成，停在这里等评审。下一步是 Phase 6：大目录检索（约 30 个系统、至少 500 张表，三级召回和 Top-5 表召回门禁）。不要做 Agent 循环。
