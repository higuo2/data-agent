from __future__ import annotations
import json
from pathlib import Path
import pytest
from enterprise_data_agent.catalog import Catalog
from enterprise_data_agent.planner import QueryPlanner

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "enterprise-data-agent-fixture"


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load(FIXTURE / "benchmark/catalog.json", FIXTURE / "benchmark/semantic_model.json")


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def planner(catalog, config) -> QueryPlanner:
    return QueryPlanner(catalog, config)


def test_buyer_city_question_plans_and_executes(planner):
    result = planner.plan("按买方城市统计已签合同金额", execute=True)
    assert result.executable is True
    assert result.validation.valid is True
    plan = result.plan
    assert plan.query_mode == "aggregate"
    assert plan.fact["model_id"] == "contracts.contract_header"
    assert plan.metrics[0]["metric_id"] == "signed_contract_amount"
    assert plan.joins[0]["relationship_id"] == "contract_to_party"
    assert plan.joins[0]["role_filter"]["value"] == "BUYER"
    assert plan.dimensions[0]["dimension_id"] == "buyer_city"
    assert result.execution["ok"] is True
    assert result.execution["row_count"] > 0
    assert any(row[1] == "香港" and row[2] == "HKD" and row[0] > 0 for row in result.execution["rows"])


def test_identifier_amount_keeps_certified_filter(planner):
    result = planner.plan("SK-0001 的含税合同金额是多少", execute=True)
    assert result.executable is True
    assert any(f["value"] == "SK-0001" for f in result.plan.filters)
    assert any(f["value"] == "SIGNED" for f in result.plan.filters)
    assert result.execution["ok"] is True
    assert result.execution["row_count"] == 1


def test_ambiguous_city_is_not_executed(planner):
    result = planner.plan("香港地区的已签合同金额是多少", execute=True)
    assert result.decision == "clarify"
    assert result.executable is False
    assert result.execution is None
    assert result.plan.ambiguity_status == "unresolved"
    assert result.validation.valid is False
    assert "unresolved_ambiguity" in result.validation.reasons


def test_smalltalk_does_not_plan(planner):
    result = planner.plan("你好", execute=True)
    assert result.decision == "smalltalk"
    assert result.plan is None
    assert result.execution is None


def test_mutation_does_not_plan(planner):
    result = planner.plan("删除合同 SK-0001", execute=True)
    assert result.decision == "unsafe"
    assert result.plan is None
    assert result.execution is None


def test_identifier_status_is_point_lookup(planner):
    result = planner.plan("查一下 SK-0001 的状态", execute=True)
    assert result.executable is True
    assert result.plan.query_mode == "point_lookup"
    assert result.plan.filters[0]["value"] == "SK-0001"
    assert result.execution["row_count"] == 1
    columns = result.execution["columns"]
    assert result.execution["rows"][0][columns.index("contract_number")] == "SK-0001"


def test_limit_comes_from_config(catalog, config):
    cfg = json.loads(json.dumps(config))
    cfg["planner"]["default_limit"] = 7
    result = QueryPlanner(catalog, cfg).plan("已签合同金额是多少")
    assert result.executable is True
    assert result.plan.limit == 7


def test_currency_mention_is_config_driven(catalog, config):
    mentioned = QueryPlanner(catalog, config).plan("当前已签合同金额（只统计港币）是多少")
    assert mentioned.plan.currency_policy["mode"] == "single_currency"
    assert mentioned.plan.currency_policy["currency_value"] == "HKD"
    cfg = json.loads(json.dumps(config))
    cfg["planner"]["currency_mentions"] = []
    grouped = QueryPlanner(catalog, cfg).plan("当前已签合同金额（只统计港币）是多少")
    assert grouped.plan.currency_policy["mode"] == "group_by_currency"


def test_non_physical_metric_is_refused(planner):
    result = planner.plan("订单净销售额是多少", execute=True)
    assert result.decision == "refused"
    assert result.reason == "unsupported_metric_shape"
    assert result.plan is None
    assert result.execution is None


def test_cross_system_dimension_is_refused(planner):
    result = planner.plan("联系人城市的已签合同金额是多少", execute=True)
    assert result.decision == "refused"
    assert result.reason == "dimension_not_reachable"
    assert result.execution is None


def test_configured_time_phrase_is_bound_in_sql(planner):
    result = planner.plan("去年的签约金额是多少", execute=True)
    assert result.executable is True
    assert result.plan.time["start"] == "2025-01-01"
    assert result.plan.time["end_exclusive"] == "2026-01-01"
    assert result.plan.time["timezone"] == "Asia/Hong_Kong"
    assert ">=" in result.execution["sql"] and "<" in result.execution["sql"]
    assert "2025-01-01" in result.execution["params"]
    assert "2026-01-01" in result.execution["params"]


def test_metric_alias_must_occur_as_written(planner):
    result = planner.plan("去年签署的合同总金额是多少", execute=True)
    assert result.decision == "refused"
    assert result.reason == "insufficient_evidence"
    assert result.execution is None
