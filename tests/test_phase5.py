from __future__ import annotations
import json
from pathlib import Path
import pytest
from enterprise_data_agent.catalog import Catalog
from enterprise_data_agent.planner import QueryPlanner
from enterprise_data_agent.queryplan import (
    QueryPlan,
    QueryPlanValidator,
    ISSUE_FANOUT_RISK,
)

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


def test_q005_current_version_does_not_sum_revisions(planner):
    result = planner.plan(_questions()["Q005"]["question"], execute=True)
    assert result.executable is True
    assert result.plan.metrics[0]["metric_id"] == "current_contract_version_amount"
    assert result.plan.fact["model_id"] == "contracts.contract_version"
    assert any(f["op"] == "eq" and f["value"] in (1, "1") for f in result.plan.filters)
    assert any(f.get("value") == "SK-0001" for f in result.plan.filters)
    assert result.execution["ok"] is True
    assert result.execution["row_count"] >= 1
    assert "contract_version" in result.execution["sql"]


def test_version_join_without_current_filter_still_fanout(validator):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [{"relationship_id": "contract_to_version", "dimension_model_id": "contracts.contract_version",
                   "cardinality_from_fact": "one_to_many"}],
        "metrics": [{"metric_id": "signed_contract_amount", "aggregate": "sum",
                     "field_id": "contracts.contract_header.signed_amount_tax_included"}],
        "dimensions": [],
        "filters": [{"field_id": "contracts.contract_header.status", "op": "eq", "value": "SIGNED"}],
        "time": None,
        "currency_policy": {"mode": "group_by_currency",
                            "currency_field_id": "contracts.contract_header.currency"},
        "resolved_slots": {"metric": {"value": "signed_contract_amount",
                                      "resolution": "certified_definition", "evidence_id": "m:v1"}},
        "ambiguity_status": "resolved", "limit": 100,
    })
    issues = validator.validate(plan)
    assert issues.valid is False
    assert ISSUE_FANOUT_RISK in issues.reasons


def test_duplicate_current_rows_are_rejected_at_execute(catalog, config):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts",
        "fact": {"model_id": "contracts.contract_version", "grain": "one row per contract version"},
        "joins": [],
        "metrics": [{"metric_id": "current_contract_version_amount", "aggregate": "sum",
                     "field_id": "contracts.contract_version.signed_amount_tax_included"}],
        "dimensions": [],
        "filters": [{"field_id": "contracts.contract_version.is_current", "op": "eq", "value": 1}],
        "time": None, "currency_policy": None,
        "resolved_slots": {"metric": {"value": "current_contract_version_amount",
                                      "resolution": "certified_definition", "evidence_id": "m:v1"}},
        "ambiguity_status": "resolved", "limit": 100,
        "unique_key": "change_type",
    })
    execution = QueryPlanner(catalog, config).executor.execute(plan)
    assert execution["ok"] is False
    assert "fanout_risk" in execution["issues"]


def test_q016_net_sales_runs_two_read_only_plans(planner):
    result = planner.plan(_questions()["Q016"]["question"], execute=True)
    assert result.executable is True
    assert result.plan.query_mode == "composite"
    parts = result.execution["sql"].split(";")
    assert len(parts) == 2
    assert "refunds" not in parts[0].lower() or "orders" in parts[0].lower()
    assert result.execution["ok"] is True
    assert "currency" in result.execution["columns"]


def test_q021_as_of_is_equality_not_snapshot_join(planner):
    result = planner.plan(_questions()["Q021"]["question"], execute=True)
    assert result.executable is True
    assert result.plan.fact["model_id"] == "finance.receivables"
    assert any(
        f["field_id"].endswith(".as_of_date") and f["op"] == "eq" and f["value"] == "2025-12-31"
        for f in result.plan.filters
    )
    assert result.plan.joins == ()
    assert "receivables" in result.execution["sql"]
    assert result.execution["ok"] is True
    assert result.execution["row_count"] >= 1


def test_snapshot_join_is_still_rejected(validator):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "finance",
        "fact": {"model_id": "finance.invoices", "grain": "one row per invoice"},
        "joins": [{"relationship_id": "invoice_to_receivable_snapshot",
                   "dimension_model_id": "finance.receivables",
                   "cardinality_from_fact": "one_to_many_over_time"}],
        "metrics": [],
        "dimensions": [], "filters": [], "time": None, "currency_policy": None,
        "resolved_slots": {}, "ambiguity_status": "not_applicable", "limit": 1,
        "query_mode": "point_lookup",
    })
    issues = validator.validate(plan)
    assert issues.valid is False
    assert ISSUE_FANOUT_RISK in issues.reasons


def test_q006_maps_through_validity_window_not_sql_join(planner):
    result = planner.plan(_questions()["Q006"]["question"], execute=True)
    assert result.executable is True
    assert result.plan.entity_mapping["entity_id"] == "customer"
    assert result.plan.entity_mapping["as_of"] == "2026-11-24"
    sql = result.execution["sql"]
    assert "customer_id_map" not in sql
    assert "crm" not in sql.lower()
    assert result.execution["ok"] is True
    assert result.execution["row_count"] == 1
    assert result.plan.order_by == ()


def test_mapping_as_of_is_config_driven(catalog, config):
    cfg = json.loads(json.dumps(config))
    cfg["planner"]["as_of_date"] = "2020-01-01"
    result = QueryPlanner(catalog, cfg).plan(_questions()["Q006"]["question"], execute=True)
    assert result.plan.entity_mapping["as_of"] == "2020-01-01"


def test_order_item_join_still_fanout(validator):
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
    issues = validator.validate(plan)
    assert issues.valid is False
    assert ISSUE_FANOUT_RISK in issues.reasons
