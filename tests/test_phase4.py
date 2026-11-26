from __future__ import annotations
import json
from pathlib import Path
import pytest
from enterprise_data_agent.catalog import Catalog
from enterprise_data_agent.planner import QueryPlanner
from enterprise_data_agent.queryplan import (
    QueryPlan,
    QueryPlanValidator,
    ISSUE_TIME_POLICY_REQUIRED,
    ISSUE_UNKNOWN_FIELD,
    ISSUE_QUERY_BUDGET_EXCEEDED,
)
from enterprise_data_agent.slots import SessionState, extract_slots

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "enterprise-data-agent-fixture"
QUESTIONS = FIXTURE / "benchmark" / "questions.jsonl"


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load(FIXTURE / "benchmark/catalog.json", FIXTURE / "benchmark/semantic_model.json")


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def planner(catalog, config) -> QueryPlanner:
    return QueryPlanner(catalog, config)


@pytest.fixture(scope="module")
def validator(catalog, config) -> QueryPlanValidator:
    return QueryPlanValidator(catalog, config)


def _questions() -> dict[str, dict]:
    rows = {}
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            rows[item["id"]] = item
    return rows


def _signed_plan(**over) -> QueryPlan:
    base = {
        "version": 1,
        "source_id": "contracts",
        "query_mode": "aggregate",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [],
        "metrics": [{
            "metric_id": "signed_contract_amount",
            "aggregate": "sum",
            "field_id": "contracts.contract_header.signed_amount_tax_included",
        }],
        "dimensions": [],
        "filters": [{"field_id": "contracts.contract_header.status", "op": "eq", "value": "SIGNED"}],
        "time": None,
        "currency_policy": {
            "mode": "group_by_currency",
            "currency_field_id": "contracts.contract_header.currency",
        },
        "resolved_slots": {
            "metric": {
                "value": "signed_contract_amount",
                "resolution": "certified_definition",
                "evidence_id": "metric:signed_contract_amount:v1",
            }
        },
        "ambiguity_status": "resolved",
        "limit": 100,
    }
    base.update(over)
    return QueryPlan.from_json(base)


def test_q009_month_bucket_on_signed_at_and_currency(planner):
    result = planner.plan(_questions()["Q009"]["question"], execute=True)
    assert result.executable is True
    assert result.plan.time_bucket["grain"] == "month"
    assert result.plan.time_bucket["field_id"] == "contracts.contract_header.signed_at"
    assert result.plan.time["start"] == "2026-01-01"
    assert result.plan.currency_policy["mode"] == "group_by_currency"
    sql = result.execution["sql"]
    assert "strftime('%Y-%m'" in sql
    assert "signed_at" in sql
    assert "GROUP BY" in sql
    assert result.execution["ok"] is True


def test_q031_in_filter_requires_buyer_role(planner):
    result = planner.plan(_questions()["Q031"]["question"], execute=True)
    assert result.executable is True
    assert result.plan.dimensions[0]["dimension_id"] == "buyer_city"
    assert result.plan.joins[0]["role_filter"]["value"] == "BUYER"
    city_filter = next(f for f in result.plan.filters if f["op"] == "in")
    assert set(city_filter["value"]) == {"华南", "香港"}
    assert " IN (" in result.execution["sql"]
    assert result.execution["ok"] is True
    assert all(row[1] in {"华南", "香港"} for row in result.execution["rows"])


def test_in_values_without_role_still_clarify(planner):
    result = planner.plan("比较华南和香港的签约合同额", execute=True)
    assert result.decision == "clarify"
    assert result.executable is False
    assert result.execution is None


def test_q033_comparison_on_certified_measure_keeps_currency(planner):
    result = planner.plan(_questions()["Q033"]["question"], execute=True)
    assert result.executable is True
    assert result.plan.metrics[0]["metric_id"] == "signed_contract_amount"
    assert any(f["op"] == "gt" and f["value"] == 100000 for f in result.plan.filters)
    assert any(f["value"] == "SIGNED" for f in result.plan.filters)
    assert result.plan.currency_policy["mode"] == "group_by_currency"
    assert ">" in result.execution["sql"]
    assert result.execution["ok"] is True
    assert "currency" in result.execution["columns"]


def test_q040_still_clarifies_before_top_n(planner):
    result = planner.plan(_questions()["Q040"]["question"], execute=True)
    assert result.decision == "clarify"
    assert result.executable is False
    assert result.execution is None
    assert result.slots.top_n == 10
    assert result.plan is None or not result.plan.order_by


def test_top_n_after_certified_metric(planner):
    result = planner.plan("按买方城市统计已签合同金额前 3", execute=True)
    assert result.executable is True
    assert result.plan.limit == 3
    assert result.plan.order_by == ({"target": "metric_0", "descending": True},)
    sql = result.execution["sql"]
    assert 'ORDER BY "metric_0" DESC' in sql
    assert sql.endswith("LIMIT 3")
    assert result.execution["row_count"] <= 3


def test_conflicting_time_bucket_phrases_are_unmatched(planner):
    result = planner.plan("已签合同金额按月按年趋势", execute=True)
    assert result.decision == "refused"
    assert "unmatched_constraint:time_bucket" in result.reason
    assert result.execution is None


def test_time_bucket_phrases_are_config_driven(catalog, config):
    question = "今年每月签约金额趋势？"
    default = QueryPlanner(catalog, config).plan(question, execute=True)
    assert default.plan.time_bucket["grain"] == "month"
    cfg = json.loads(json.dumps(config))
    cfg["planner"]["time_bucket_phrases"] = [{"phrase": "每月", "grain": "year"}]
    changed = QueryPlanner(catalog, cfg).plan(question, execute=True)
    assert changed.executable is True
    assert changed.plan.time_bucket["grain"] == "year"
    assert "strftime('%Y'" in changed.execution["sql"]
    assert "strftime('%Y-%m'" not in changed.execution["sql"]


def test_comparison_patterns_are_config_driven(catalog, config):
    question = _questions()["Q033"]["question"]
    default = QueryPlanner(catalog, config).plan(question, execute=True)
    assert default.executable is True
    assert any(f["op"] == "gt" for f in default.plan.filters)
    cfg = json.loads(json.dumps(config))
    cfg["planner"]["comparison_patterns"] = [{"phrase": "超过", "op": "gt"}]
    changed = QueryPlanner(catalog, cfg).plan(question, execute=True)
    assert changed.executable is True
    assert not any(f["op"] == "gt" for f in changed.plan.filters)


def test_metric_ngram_overlap_is_config_driven(catalog, config):
    question = _questions()["Q033"]["question"]
    default = QueryPlanner(catalog, config).plan(question, execute=True)
    assert default.plan.metrics[0]["metric_id"] == "signed_contract_amount"
    cfg = json.loads(json.dumps(config))
    cfg["slot_filling"]["min_metric_ngram_overlap"] = 1.0
    blocked = QueryPlanner(catalog, cfg).plan(question, execute=True)
    assert blocked.decision == "refused"
    assert blocked.reason == "insufficient_evidence"
    assert blocked.execution is None


def test_top_n_patterns_are_config_driven(catalog, config):
    question = "已签合同金额前 3"
    default = QueryPlanner(catalog, config).plan(question, execute=True)
    assert default.plan.limit == 3
    cfg = json.loads(json.dumps(config))
    cfg["planner"]["top_n_patterns"] = [{"regex": "top\\s*(\\d+)"}]
    changed = QueryPlanner(catalog, cfg).plan(question, execute=True)
    assert changed.executable is True
    assert changed.plan.limit == config["planner"]["default_limit"]
    assert changed.plan.order_by == ()


def test_in_connectors_are_config_driven(catalog, config):
    question = _questions()["Q031"]["question"]
    default = QueryPlanner(catalog, config).plan(question, execute=True)
    assert any(f["op"] == "in" for f in default.plan.filters)
    cfg = json.loads(json.dumps(config))
    cfg["slot_filling"]["value_connectors"] = ["vs"]
    changed = QueryPlanner(catalog, cfg).plan(question, execute=True)
    assert changed.executable is True
    city_filters = [f for f in changed.plan.filters if f["field_id"].endswith(".city")]
    assert city_filters == [] or (len(city_filters) == 1 and city_filters[0]["value"] == "香港")


def test_injected_comparison_cannot_name_a_field(planner):
    with pytest.raises(ValueError, match="field_id"):
        planner.plan(
            "已签合同金额是多少",
            slots={"metric_ids": ["signed_contract_amount"], "comparisons": [
                {"op": "gt", "value": 1, "field_id": "contracts.contract_header.signed_amount_tax_included"}
            ]},
        )


def test_over_budget_top_n_is_rejected(planner):
    result = planner.plan(
        "已签合同金额是多少",
        execute=True,
        slots={"metric_ids": ["signed_contract_amount"], "top_n": 5000},
    )
    assert result.decision == "refused"
    assert ISSUE_QUERY_BUDGET_EXCEEDED in result.reason
    assert result.execution is None


def test_validator_rejects_time_bucket_off_ontology_time_role(validator):
    plan = _signed_plan(time_bucket={
        "field_id": "contracts.contract_header.expiry_at",
        "grain": "month",
    })
    issues = validator.validate(plan)
    assert issues.valid is False
    assert ISSUE_TIME_POLICY_REQUIRED in issues.reasons


def test_validator_rejects_empty_in_filter(validator):
    plan = _signed_plan(filters=[
        {"field_id": "contracts.contract_header.status", "op": "eq", "value": "SIGNED"},
        {"field_id": "contracts.contract_header.currency", "op": "in", "value": []},
    ])
    issues = validator.validate(plan)
    assert issues.valid is False
    assert ISSUE_UNKNOWN_FIELD in issues.reasons


def test_validator_rejects_order_by_off_metric(validator):
    plan = _signed_plan(order_by=[{"target": "dim_0", "descending": True}])
    issues = validator.validate(plan)
    assert issues.valid is False
    assert ISSUE_UNKNOWN_FIELD in issues.reasons


def test_extract_slots_reads_operators(catalog, config):
    slots = extract_slots("比较华南和香港的买方签约合同额前 5，金额大于 100000", catalog.ontology, config)
    assert slots.metric_ids == ("signed_contract_amount",)
    assert slots.dimension_ids == ("buyer_city",)
    assert slots.in_values == ("香港", "华南")
    assert slots.comparisons == ({"op": "gt", "value": 100000},)
    assert slots.top_n == 5
    assert slots.unmatched_constraints == ()
