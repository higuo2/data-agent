from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import pytest
from enterprise_data_agent.catalog import Catalog
from enterprise_data_agent.planner import QueryPlanner
from enterprise_data_agent.preflight import Preflight
from enterprise_data_agent.queryplan import ISSUE_FANOUT_RISK, ISSUE_RELATIONSHIP_NOT_APPROVED, QueryPlan, QueryPlanValidator
from enterprise_data_agent.retrieval import SemanticRetriever

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "enterprise-data-agent-fixture"
QUESTIONS = FIXTURE / "benchmark" / "questions.jsonl"
CORE_CATALOG = FIXTURE / "benchmark" / "catalog.json"
CORE_ONTOLOGY = FIXTURE / "benchmark" / "semantic_model.json"

RECALL_CASES = [
    ("去年签署的合同总金额是多少？", ["contracts.contract_header"]),
    ("SK-0001 最新合同版本金额是多少？", ["contracts.contract_version"]),
    ("订单净销售额是多少？", ["sales.orders"]),
    ("截至 2025-12-31 的应收账款余额是多少？", ["finance.receivables"]),
    ("按买方城市统计已签合同金额。", ["contracts.contract_header"]),
    ("查找金额大于 100000 的合同", ["contracts.contract_header"]),
    ("哪个客户的已签合同额最高？", ["contracts.contract_header"]),
    ("今年每月签约金额趋势？", ["contracts.contract_header"]),
    ("SK-0001 的含税合同金额是多少？", ["contracts.contract_header"]),
]

# Coarse black-box buckets for the 40-question fixture. Range-out answers
# are `refuse` rather than execute. Routing and clarify must not query.
EXPECTED_BUCKET = {
    "Q001": "execute",
    "Q002": "execute",
    "Q003": "clarify",
    "Q004": "execute",
    "Q005": "execute",
    "Q006": "execute",
    "Q007": "execute",
    "Q008": "execute",
    "Q009": "execute",
    "Q010": "refuse",
    "Q011": "refuse",
    "Q012": "refuse",
    "Q013": "execute",
    "Q014": "execute",
    "Q015": "refuse",
    "Q016": "execute",
    "Q017": "refuse",
    "Q018": "refuse",
    "Q019": "refuse",
    "Q020": "refuse",
    "Q021": "execute",
    "Q022": "execute",
    "Q023": "refuse",
    "Q024": "refuse",
    "Q025": "unsupported",
    "Q026": "smalltalk",
    "Q027": "needs_classification",
    "Q028": "unsafe",
    "Q029": "execute",
    "Q030": "refuse",
    "Q031": "execute",
    "Q032": "execute",
    "Q033": "execute",
    "Q034": "execute",
    "Q035": "execute",
    "Q036": "refuse",
    "Q037": "refuse",
    "Q038": "clarify",
    "Q039": "execute",
    "Q040": "clarify",
}


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))


def _load_generate():
    path = ROOT / "testdata" / "large-catalog" / "generate.py"
    spec = importlib.util.spec_from_file_location("large_catalog_generate", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def large_root(tmp_path_factory):
    dest = tmp_path_factory.mktemp("large-catalog")
    return _load_generate().write_fixture(dest, CORE_CATALOG, CORE_ONTOLOGY)


@pytest.fixture(scope="module")
def large_catalog(large_root) -> Catalog:
    return Catalog.load(large_root / "benchmark/catalog.json", large_root / "benchmark/semantic_model.json")


@pytest.fixture(scope="module")
def small_catalog() -> Catalog:
    return Catalog.load(CORE_CATALOG, CORE_ONTOLOGY)


@pytest.fixture(scope="module")
def planner(small_catalog, config) -> QueryPlanner:
    return QueryPlanner(small_catalog, config)


def _questions() -> dict[str, dict]:
    rows = {}
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            rows[item["id"]] = item
    return rows


def _bucket(result) -> str:
    decision = str(result.decision or "")
    if decision in {"smalltalk", "unsafe", "unsupported", "clarify", "execute", "needs_classification"}:
        return decision
    if decision == "refused":
        return "refuse"
    return decision


def test_large_catalog_has_30_systems_and_500_tables(large_catalog):
    assert len(large_catalog.systems) == 30
    assert len(large_catalog.tables) >= 500
    names = {t.name for t in large_catalog.tables}
    assert "orders" in names and "contract_header" in names
    assert any(t.deprecated and t.ref == "legacy_contracts.contract_header" for t in large_catalog.tables)
    domains = {t.domain for t in large_catalog.tables}
    assert "contract_lifecycle" in domains
    assert "human_resources" in domains


def test_top5_table_recall_on_large_catalog(large_catalog, config):
    retriever = SemanticRetriever(large_catalog, config["retrieval"])
    hits = 0
    gold_total = 0
    for question, gold in RECALL_CASES:
        _, tables = retriever.retrieve(question)
        refs = [item.ref for item in tables[:5]]
        for table in gold:
            gold_total += 1
            if table in refs:
                hits += 1
        assert gold[0] in refs, (question, refs)
    assert hits / gold_total == 1.0


def test_deprecated_header_does_not_outrank_gold(large_catalog, config):
    _, tables = SemanticRetriever(large_catalog, config["retrieval"]).retrieve(
        "去年签署的合同总金额是多少？"
    )
    refs = [item.ref for item in tables]
    assert "contracts.contract_header" in refs
    if "legacy_contracts.contract_header" in refs:
        assert refs.index("contracts.contract_header") < refs.index("legacy_contracts.contract_header")


def test_retrieve_stays_inside_selected_systems(large_catalog, config):
    systems, tables = SemanticRetriever(large_catalog, config["retrieval"]).retrieve(
        "订单净销售额是多少？"
    )
    allowed = {item["system_id"] for item in systems}
    assert len(systems) <= config["retrieval"]["top_systems"]
    assert len(tables) <= config["retrieval"]["top_tables"]
    assert all(item.system_id in allowed for item in tables)
    assert "sales" in allowed


def test_top_systems_config_changes_retrieval(large_catalog, config):
    question = "合同金额和已回款金额哪个更高？"
    default = SemanticRetriever(large_catalog, config["retrieval"]).retrieve(question)
    cfg = json.loads(json.dumps(config))
    cfg["retrieval"]["top_systems"] = 1
    narrowed = SemanticRetriever(large_catalog, cfg["retrieval"]).retrieve(question)
    assert len(default[0]) > 1
    assert len(narrowed[0]) == 1


def test_deprecated_penalty_is_config_driven(large_catalog, config):
    question = "去年签署的合同总金额是多少？"
    cfg = json.loads(json.dumps(config))
    cfg["retrieval"]["deprecated_penalty"] = 0
    cfg["retrieval"]["minimum_lexical_score"] = 0
    cfg["retrieval"]["per_system_tables"] = 20
    zero = SemanticRetriever(large_catalog, cfg["retrieval"]).search(
        question, 40, {"legacy_contracts", "contracts"}
    )
    cfg["retrieval"]["deprecated_penalty"] = 5
    penalized = SemanticRetriever(large_catalog, cfg["retrieval"]).search(
        question, 40, {"legacy_contracts", "contracts"}
    )
    refs_zero = {item.ref: item.score for item in zero}
    refs_penalized = {item.ref: item.score for item in penalized}
    assert "legacy_contracts.contract_header" in refs_zero, sorted(refs_zero)
    assert refs_penalized.get("legacy_contracts.contract_header", float("-inf")) < refs_zero["legacy_contracts.contract_header"]


def test_preflight_uses_three_level_retrieve(large_catalog, config):
    result = Preflight(large_catalog, config).inspect("SK-0001 的含税合同金额是多少？")
    assert result["selected_system"] == "contracts"
    refs = [item["table"] for item in result["candidate_tables"]]
    assert "contracts.contract_header" in refs
    assert len(result["candidates"]["systems"]) <= config["retrieval"]["top_systems"]


def test_order_item_join_still_fanout(small_catalog, config):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "sales",
        "fact": {"model_id": "sales.orders", "grain": "one row per order"},
        "joins": [{"relationship_id": "order_to_items", "dimension_model_id": "sales.order_items",
                   "cardinality_from_fact": "one_to_many"}],
        "metrics": [{"metric_id": "booked_order_amount", "aggregate": "sum",
                     "field_id": "sales.orders.order_total"}],
        "dimensions": [], "filters": [], "time": None,
        "currency_policy": {"mode": "group_by_currency", "currency_field_id": "sales.orders.currency"},
        "resolved_slots": {"metric": {"value": "booked_order_amount",
                                      "resolution": "certified_definition", "evidence_id": "m:v1"}},
        "ambiguity_status": "resolved", "limit": 100,
    })
    issues = QueryPlanValidator(small_catalog, config).validate(plan)
    assert issues.valid is False
    assert ISSUE_FANOUT_RISK in issues.reasons


def test_candidate_relationship_cannot_enter_compiler(small_catalog, config):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "crm",
        "fact": {"model_id": "crm.customers", "grain": "one row per canonical customer"},
        "joins": [{"relationship_id": "customer_to_contract_source_id",
                   "dimension_model_id": "contracts.contract_header",
                   "cardinality_from_fact": "one_to_many"}],
        "metrics": [], "dimensions": [], "filters": [], "time": None, "currency_policy": None,
        "resolved_slots": {}, "ambiguity_status": "not_applicable", "limit": 1,
        "query_mode": "point_lookup",
    })
    issues = QueryPlanValidator(small_catalog, config).validate(plan)
    assert issues.valid is False
    assert ISSUE_RELATIONSHIP_NOT_APPROVED in issues.reasons


def test_q014_does_not_execute_fanout_plan(planner):
    result = planner.plan("订单 O0001 的商品明细和订单总额是多少？", execute=True)
    joins = result.plan.joins if result.plan else ()
    assert all(item.get("relationship_id") != "order_to_items" for item in joins)


@pytest.mark.parametrize("qid", sorted(EXPECTED_BUCKET))
def test_benchmark_behavior_matrix(planner, qid):
    item = _questions()[qid]
    result = planner.plan(item["question"], execute=True)
    assert _bucket(result) == EXPECTED_BUCKET[qid]
    if EXPECTED_BUCKET[qid] in {"clarify", "smalltalk", "unsafe", "unsupported", "refuse", "needs_classification"}:
        assert result.executable is False
        assert result.execution is None
