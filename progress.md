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

Phase 1 已完成。Phase 2 已完成查询计划的校验、只读执行和规划器。问句会先做预检，能确定时生成一份有界 `QueryPlan` 并只读执行；角色或口径没定、或形状超出本阶段时拒绝执行。还没有多轮对话和 Agent 循环。

当前边界：一张事实表，加上目录批准、且不会把度量乘多行的维度；聚合只有 `SUM`、`COUNT`、`AVG`；一个计划只用一个物理数据源。不生成嵌套 SQL、CTE、窗口函数或跨系统连接。

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

### 证据

- 2026-11-24：`/opt/anaconda3/bin/pytest -q` → **76 passed**（Phase 1 共 26 个，Phase 2 校验与执行 38 个，规划器 12 个）。
- 规划器用真实 SQLite 执行了「按买方城市统计已签合同金额」和 `SK-0001` 的含税金额；香港角色不清、闲聊和删除请求都没有查库。


## 未完成

### Phase 2 明确不做

- 链式连接、版本快照（`contract_to_version`）、预聚合节点、事实表连事实表。
- 按月或趋势分桶。时间维度别名命中时直接拒绝，不把原始时间戳当成月份。
- 指标别名必须原样出现在问句里。「合同总金额」不会自动当成「合同金额」。
- 城市等自由文本值不从问句里抽出来做过滤。已明确的维度会分组，不会猜一个城市值。
- 度量不是物理列、认证过滤写不成单列等值、或维度要跨系统连接时，拒绝而不改写 SQL。

### Phase 3 及以后

- Phase 3：UModel / Auto Ontology 适配器、多轮澄清并记住用户选择、工具注册、有界 Agent 循环、结果后处理、有界重试。
- Phase 4：身份传递、行列权限、密钥、审计、预算、取消、schema 漂移、评测门禁。
- Phase 5：扩大测试数据，评测表召回、实体链接、聚合正确性、澄清、安全和延迟，再接入更多系统。

### 现在还不能声称的能力

- 没有大模型意图分类。规划器只覆盖别名能对上、且形状在本阶段边界内的问句。
- 检索仍是词法规则，还没有和 Auto Ontology 比过召回。
- 实体只有精确匹配。模糊匹配不能直接当答案。
- 没有会话记忆、生产认证、审计或权限。
- 没有接入真实的 UModel / Auto Ontology 网络服务。

## 决策

- 基线是招聘领域的 `Hoiikk/umodel-agent-platform`。它的字段和追问规则写在代码里，不适合大目录发现和受治理的分析，因此在本目录干净重写，不改原仓库。
- 语义基础先用 UModel + NVIDIA Auto Ontology。开发数据用 `testdata/enterprise-data-agent-fixture/`。
- 可变的业务词、权重和阈值放配置或语义模型；代码只保留算法和不变量。

## 下一步

Phase 2 的规划器已完成，停在这里等评审。下一步才是 Phase 3：多轮澄清并记住用户的选择，以及 UModel / Auto Ontology 适配器。在那之前不要做对话循环，也不要放宽 SQL 形状。
