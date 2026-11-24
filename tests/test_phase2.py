from __future__ import annotations
import json
import copy
from pathlib import Path
import pytest
from enterprise_data_agent.catalog import Catalog
from enterprise_data_agent.executor import QueryExecutor
from enterprise_data_agent.queryplan import (
    QueryPlan,
    QueryPlanValidator,
    ISSUE_CROSS_SOURCE_PLAN,
    ISSUE_FANOUT_RISK,
    ISSUE_UNRESOLVED_AMBIGUITY,
    ISSUE_METRIC_NOT_CERTIFIED,
    ISSUE_UNKNOWN_FIELD,
    ISSUE_UNKNOWN_MODEL,
    ISSUE_UNSUPPORTED_AGGREGATE,
    ISSUE_CURRENCY_POLICY_REQUIRED,
    ISSUE_TIME_POLICY_REQUIRED,
    ISSUE_QUERY_BUDGET_EXCEEDED,
    ISSUE_RELATIONSHIP_NOT_APPROVED,
    ISSUE_GRAIN_MISMATCH,
    ISSUE_EMPTY_METRIC_SET,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "enterprise-data-agent-fixture"


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load(FIXTURE / "benchmark/catalog.json", FIXTURE / "benchmark/semantic_model.json")


@pytest.fixture(scope="module")
def validator(catalog) -> QueryPlanValidator:
    cfg = json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))
    return QueryPlanValidator(catalog, cfg)


def signed_amount_plan(**over) -> QueryPlan:
    """A valid snowflake plan: contracts.contract_header fact + buyer party role-filtered
    dimension, using the certified signed_contract_amount metric. This is the
    accept side of the Phase 2 contract examples."""
    base = {
        "version": 1,
        "source_id": "contracts",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [{
            "relationship_id": "contract_to_party",
            "dimension_model_id": "contracts.contract_party",
            "cardinality_from_fact": "one_to_one_after_role_filter",
            "role_filter": {"field_id": "contracts.contract_party.party_role", "op": "eq", "value": "BUYER"},
        }],
        "metrics": [{
            "metric_id": "signed_contract_amount",
            "aggregate": "sum",
            "field_id": "contracts.contract_header.signed_amount_tax_included",
        }],
        "dimensions": [{"dimension_id": "buyer_city", "field_id": "contracts.contract_party.city"}],
        "filters": [{"field_id": "contracts.contract_header.status", "op": "eq", "value": "SIGNED"}],
        "time": {
            "field_id": "contracts.contract_header.signed_at",
            "start": "2025-01-01",
            "end_exclusive": "2026-01-01",
            "timezone": "Asia/Hong_Kong",
        },
        "currency_policy": {
            "mode": "group_by_currency",
            "currency_field_id": "contracts.contract_header.currency",
        },
        "resolved_slots": {
            "metric": {"value": "signed_contract_amount", "resolution": "certified_definition", "evidence_id": "metric:signed_contract_amount:v1"},
            "buyer_city": {"value": "buyer_city", "resolution": "user_confirmed", "evidence_id": "turn:2"},
        },
        "ambiguity_status": "resolved",
        "limit": 100,
    }
    base.update(over)
    return QueryPlan.from_json(base)


def reasons(validation):
    return [i.issue_type for i in validation.issues]


# ---- accept side -----------------------------------------------------------

def test_valid_signs_snowflake_plan_passes(validator):
    plan = validator.validate(signed_amount_plan())
    assert plan.valid is True
    assert plan.issues == ()
    assert plan.fanout_risk is False
    assert plan.cross_source is False


def test_valid_plan_without_dimension_or_time_passes(validator):
    plan = signed_amount_plan(
        dimensions=[],
        time=None,
        ambiguity_status="resolved",
        resolved_slots={"metric": {"value": "signed_contract_amount", "resolution": "certified_definition", "evidence_id": "m:v1"}},
    )
    v = validator.validate(plan)
    assert v.valid is True


def test_single_currency_policy_accepted(validator):
    plan = signed_amount_plan(currency_policy={
        "mode": "single_currency",
        "currency_field_id": "contracts.contract_header.currency",
        "currency_value": "HKD",
    })
    v = validator.validate(plan)
    assert v.valid is True


# ---- fan-out protection ---------------------------------------------------

def test_join_with_fanout_warning_is_rejected(validator):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "sales",
        "fact": {"model_id": "sales.orders", "grain": "one row per order"},
        "joins": [{"relationship_id": "order_to_items", "dimension_model_id": "sales.order_items", "cardinality_from_fact": "one_to_many"}],
        "metrics": [{"metric_id": "booked_order_amount", "aggregate": "sum", "field_id": "sales.orders.order_total"}],
        "dimensions": [{"dimension_id": "contract_signed_month", "field_id": "sales.orders.order_date"}],
        "filters": [{"field_id": "sales.orders.status", "op": "eq", "value": "PAID"}],
        "time": None,
        "currency_policy": {"mode": "group_by_currency", "currency_field_id": "sales.orders.currency"},
        "resolved_slots": {"metric": {"value": "booked_order_amount", "resolution": "certified_definition", "evidence_id": "m:v1"}},
        "ambiguity_status": "resolved", "limit": 100,
    })
    v = validator.validate(plan)
    assert v.valid is False
    assert v.fanout_risk is True
    assert ISSUE_FANOUT_RISK in reasons(v)
    assert any("orders.order_total repeats once per item" in i.detail for i in v.issues)


def test_one_to_many_without_role_filter_is_rejected(validator):
    plan = signed_amount_plan(
        joins=[{"relationship_id": "contract_to_party", "dimension_model_id": "contracts.contract_party",
                "cardinality_from_fact": "one_to_many"}],
        dimensions=[],
    )
    v = validator.validate(plan)
    assert v.valid is False
    assert v.fanout_risk is True
    assert ISSUE_FANOUT_RISK in reasons(v)


def test_version_join_rejected_as_fanout(validator):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [{"relationship_id": "contract_to_version", "dimension_model_id": "contracts.contract_version",
                   "cardinality_from_fact": "one_to_many"}],
        "metrics": [{"metric_id": "signed_contract_amount", "aggregate": "sum", "field_id": "contracts.contract_header.signed_amount_tax_included"}],
        "dimensions": [],
        "filters": [], "time": None,
        "currency_policy": {"mode": "group_by_currency", "currency_field_id": "contracts.contract_header.currency"},
        "resolved_slots": {"metric": {"value": "signed_contract_amount", "resolution": "certified_definition", "evidence_id": "m:v1"}},
        "ambiguity_status": "resolved", "limit": 100,
    })
    v = validator.validate(plan)
    assert v.valid is False
    assert v.fanout_risk is True


def test_approved_role_filter_restores_single_row_and_passes(validator):
    # contract_party is 1:N from the head table, but BUYER/SELLER role filter is approved.
    plan = signed_amount_plan()
    assert validator.validate(plan).valid is True


def test_role_filter_missing_value_is_rejected(validator):
    plan = signed_amount_plan(
        joins=[{"relationship_id": "contract_to_party", "dimension_model_id": "contracts.contract_party",
                "cardinality_from_fact": "one_to_many",
                "role_filter": {"field_id": "contracts.contract_party.party_role", "op": "eq", "value": ""}}],
        dimensions=[],
    )
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_FANOUT_RISK in reasons(v)


# ---- cross source ---------------------------------------------------------

def test_cross_system_dimension_field_rejected(validator):
    # A single-source plan (source_id=contracts) that references a field on a
    # table in another system must be rejected as cross source. The joined
    # party dimension is contracts-internal; dropping the dimension field to
    # CRM exposes the cross-source reachability check.
    plan = signed_amount_plan(dimensions=[{"dimension_id": "contact_city", "field_id": "crm.contacts.city"}],
                              resolved_slots={
                                  "metric": {"value": "signed_contract_amount", "resolution": "certified_definition", "evidence_id": "m:v1"},
                                  "contact_city": {"value": "contact_city", "resolution": "user_confirmed", "evidence_id": "turn:2"},
                              })
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_CROSS_SOURCE_PLAN in reasons(v)


def test_metric_from_another_source_rejected(validator):
    # cash_collected lives in finance, but the fact is contracts.
    plan = signed_amount_plan(metrics=[{
        "metric_id": "cash_collected", "aggregate": "sum", "field_id": "finance.payment_allocations.allocated_amount",
    }])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_METRIC_NOT_CERTIFIED in reasons(v)
    assert ISSUE_CROSS_SOURCE_PLAN in reasons(v)


# ---- metric authority / aggregation ---------------------------------------

def test_unknown_metric_id_rejected(validator):
    plan = signed_amount_plan(metrics=[{"metric_id": "not_a_real_metric", "aggregate": "sum",
                                        "field_id": "contracts.contract_header.signed_amount_tax_included"}])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_METRIC_NOT_CERTIFIED in reasons(v)


def test_unsupported_aggregate_rejected(validator):
    plan = signed_amount_plan(metrics=[{"metric_id": "signed_contract_amount", "aggregate": "percentile",
                                        "field_id": "contracts.contract_header.signed_amount_tax_included"}])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNSUPPORTED_AGGREGATE in reasons(v)


def test_sum_on_text_column_rejected(validator):
    plan = signed_amount_plan(metrics=[{"metric_id": "signed_contract_amount", "aggregate": "sum",
                                        "field_id": "contracts.contract_header.title"}])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_METRIC_NOT_CERTIFIED in reasons(v)


# ---- ambiguity ------------------------------------------------------------

def test_unresolved_ambiguity_is_rejected(validator):
    plan = signed_amount_plan(ambiguity_status="unresolved", resolved_slots={})
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNRESOLVED_AMBIGUITY in reasons(v)


def test_unresolved_ambiguity_blocks_even_when_slots_are_filled(validator):
    plan = signed_amount_plan(ambiguity_status="unresolved")
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNRESOLVED_AMBIGUITY in reasons(v)


def test_not_checked_with_missing_resolution_rejected(validator):
    plan = signed_amount_plan(ambiguity_status="not_checked",
                              resolved_slots={"metric": {"value": "signed_contract_amount", "resolution": "certified_definition", "evidence_id": "m:v1"}})
    # dimension buyer_city has no resolution slot
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNRESOLVED_AMBIGUITY in reasons(v)


# ---- currency & time policy ----------------------------------------------

def test_multi_currency_fact_requires_policy(validator):
    plan = signed_amount_plan(currency_policy={"mode": "none", "currency_field_id": "contracts.contract_header.currency"})
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_CURRENCY_POLICY_REQUIRED in reasons(v)


def test_group_by_currency_requires_field(validator):
    plan = signed_amount_plan(currency_policy={"mode": "group_by_currency"})
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_CURRENCY_POLICY_REQUIRED in reasons(v)


def test_time_range_requires_timezone(validator):
    plan = signed_amount_plan(time={"field_id": "contracts.contract_header.signed_at",
                                    "start": "2025-01-01", "end_exclusive": "2026-01-01"})
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_TIME_POLICY_REQUIRED in reasons(v)


# ---- budget / limits / unknown shapes -------------------------------------

def test_limit_exceeding_budget_rejected(validator):
    plan = signed_amount_plan(limit=999999)
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_QUERY_BUDGET_EXCEEDED in reasons(v)


def test_empty_metric_set_rejected(validator):
    plan = signed_amount_plan(metrics=[])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_EMPTY_METRIC_SET in reasons(v)


def test_unknown_fact_rejected(validator):
    plan = signed_amount_plan(fact={"model_id": "bogus.does_not_exist", "grain": "x"})
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNKNOWN_MODEL in reasons(v)


def test_unknown_dimension_field_rejected(validator):
    plan = signed_amount_plan(dimensions=[{"dimension_id": "buyer_city", "field_id": "contracts.contract_party.nope"}])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNKNOWN_FIELD in reasons(v)


def test_dimension_not_reachable_rejected(validator):
    # dimension field on a table not reachable through an approved join
    plan = signed_amount_plan(dimensions=[{"dimension_id": "contact_city", "field_id": "crm.contacts.city"}])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNKNOWN_FIELD in reasons(v)


def test_unknown_relationship_rejected(validator):
    plan = signed_amount_plan(joins=[{"relationship_id": "not_in_ontology", "dimension_model_id": "contracts.contract_party",
                                      "cardinality_from_fact": "one_to_one"}])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_RELATIONSHIP_NOT_APPROVED in reasons(v)


def test_wrong_schema_version_rejected(validator):
    plan = signed_amount_plan(version=99)
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_UNKNOWN_MODEL in reasons(v)


def test_validator_recomputes_cardinality_not_trusts_plan(validator):
    # The validator recomputes join safety from the ontology, not from the plan's
    # self-declared cardinality_from_fact. order_to_items carries a fanout warning
    # in the ontology, so even a self-declared one_to_one join must be rejected.
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "sales", "query_mode": "aggregate",
        "fact": {"model_id": "sales.orders", "grain": "one row per order"},
        "joins": [{"relationship_id": "order_to_items", "dimension_model_id": "sales.order_items",
                   "cardinality_from_fact": "one_to_one",
                   "role_filter": {"field_id": "sales.order_items.status", "op": "eq", "value": "PAID"}}],
        "metrics": [{"metric_id": "booked_order_amount", "aggregate": "sum",
                     "field_id": "sales.orders.order_total"}],
        "dimensions": [], "filters": [{"field_id": "sales.orders.status", "op": "eq", "value": "PAID"}],
        "time": None,
        "currency_policy": {"mode": "group_by_currency", "currency_field_id": "sales.orders.currency"},
        "resolved_slots": {"metric": {"value": "booked_order_amount", "resolution": "certified_definition", "evidence_id": "m:v1"}},
        "ambiguity_status": "resolved", "limit": 100,
    })
    v = validator.validate(plan)
    assert v.valid is False
    assert v.fanout_risk is True
    assert ISSUE_FANOUT_RISK in reasons(v)


# ---- edge hardening: grain word-boundary, point lookup, role filter locking --

def test_grain_contract_does_not_match_contract_item(validator):
    # `contract` must NOT be accepted for `one row per contract item`: the two
    # have different word sequences. A bare substring/subset check would misfire.
    plan = signed_amount_plan(
        fact={"model_id": "contracts.contract_party", "grain": "one row per party role per contract"},
        metrics=[{"metric_id": "signed_contract_amount", "aggregate": "sum",
                  "field_id": "contracts.contract_header.signed_amount_tax_included"}],
        source_id="contracts",
    )
    # The fact is the party table here (declared grain is contract-level shorthand),
    # which no longer matches `one row per party role per contract`.
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_GRAIN_MISMATCH in reasons(v)


def test_grain_contract_matches_catalog_grain_word_boundary(validator):
    # `contract` shorthand is compatible with `one row per contract` because both
    # reduce to the SAME word sequence [contract] after stop words.
    plan = signed_amount_plan(fact={"model_id": "contracts.contract_header", "grain": "contract"})
    assert validator.validate(plan).valid is True


def test_point_lookup_with_identifier_column_passes(validator):
    # SK-0001 status lookup is a point lookup with an equality filter on the
    # ontology business identifier column (contract_number); no metrics needed.
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts", "query_mode": "point_lookup",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [], "metrics": [], "dimensions": [],
        "filters": [{"field_id": "contracts.contract_header.contract_number", "op": "eq", "value": "SK-0001"}],
        "time": None,
        "currency_policy": {"mode": "single_currency", "currency_field_id": "contracts.contract_header.currency", "currency_value": "HKD"},
        "resolved_slots": {}, "ambiguity_status": "not_applicable", "limit": 1,
    })
    v = validator.validate(plan)
    assert v.valid is True


def test_aggregate_with_no_metrics_still_rejected(validator):
    # empty_metric_set only guards aggregate mode; it must still fire here.
    plan = signed_amount_plan(metrics=[])
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_EMPTY_METRIC_SET in reasons(v)


def test_point_lookup_without_exact_filter_rejected(validator):
    # A point lookup on a non-identifier column (status) has nothing exact to pin.
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts", "query_mode": "point_lookup",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [], "metrics": [], "dimensions": [],
        "filters": [{"field_id": "contracts.contract_header.status", "op": "eq", "value": "SIGNED"}],
        "time": None,
        "currency_policy": {"mode": "single_currency", "currency_field_id": "contracts.contract_header.currency", "currency_value": "HKD"},
        "resolved_slots": {}, "ambiguity_status": "not_applicable", "limit": 1,
    })
    v = validator.validate(plan)
    assert v.valid is False
    assert "point_lookup_requires_exact_filter" in reasons(v) or ISSUE_EMPTY_METRIC_SET in reasons(v)


def test_role_filter_locked_to_ontology_role_column(validator):
    # role_filter must pin a single value on the ontology-declared role column
    # (party_role). Using city as the role filter is rejected as fan-out.
    plan = signed_amount_plan(
        joins=[{"relationship_id": "contract_to_party", "dimension_model_id": "contracts.contract_party",
                "cardinality_from_fact": "one_to_one_after_role_filter",
                "role_filter": {"field_id": "contracts.contract_party.city", "op": "eq", "value": "香港"}}],
        dimensions=[], resolved_slots={}, ambiguity_status="resolved",
    )
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_FANOUT_RISK in reasons(v)


def test_role_filter_multi_value_rejected(validator):
    # role filter must pin exactly one role (no comma list).
    plan = signed_amount_plan(
        joins=[{"relationship_id": "contract_to_party", "dimension_model_id": "contracts.contract_party",
                "cardinality_from_fact": "one_to_one_after_role_filter",
                "role_filter": {"field_id": "contracts.contract_party.party_role", "op": "eq", "value": "BUYER,SELLER"}}],
    )
    v = validator.validate(plan)
    assert v.valid is False
    assert ISSUE_FANOUT_RISK in reasons(v)


# ---- executor slice: read-only aggregate + point lookup ---------------------

@pytest.fixture(scope="module")
def executor(catalog) -> QueryExecutor:
    cfg = json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))
    return QueryExecutor(catalog, cfg)


def test_executor_rejects_invalid_plan_without_running(executor):
    # A fanout plan is rejected before any SQL is generated.
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "sales", "query_mode": "aggregate",
        "fact": {"model_id": "sales.orders", "grain": "one row per order"},
        "joins": [{"relationship_id": "order_to_items", "dimension_model_id": "sales.order_items",
                   "cardinality_from_fact": "one_to_many"}],
        "metrics": [{"metric_id": "booked_order_amount", "aggregate": "sum", "field_id": "sales.orders.order_total"}],
        "dimensions": [], "filters": [{"field_id": "sales.orders.status", "op": "eq", "value": "PAID"}],
        "time": None,
        "currency_policy": {"mode": "group_by_currency", "currency_field_id": "sales.orders.currency"},
        "resolved_slots": {"metric": {"value": "booked_order_amount", "resolution": "certified_definition", "evidence_id": "m:v1"}},
        "ambiguity_status": "resolved", "limit": 100,
    })
    res = executor.execute(plan)
    assert res["ok"] is False
    assert "fanout_risk" in res["issues"]
    assert "sql" not in res


def test_executor_aggregate_groups_by_buyer_city_and_currency(executor):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts", "query_mode": "aggregate",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [{"relationship_id": "contract_to_party", "dimension_model_id": "contracts.contract_party",
                   "cardinality_from_fact": "one_to_one_after_role_filter",
                   "role_filter": {"field_id": "contracts.contract_party.party_role", "op": "eq", "value": "BUYER"}}],
        "metrics": [{"metric_id": "signed_contract_amount", "aggregate": "sum",
                     "field_id": "contracts.contract_header.signed_amount_tax_included"}],
        "dimensions": [{"dimension_id": "buyer_city", "field_id": "contracts.contract_party.city"}],
        "filters": [{"field_id": "contracts.contract_header.status", "op": "eq", "value": "SIGNED"}],
        "time": None,
        "currency_policy": {"mode": "group_by_currency", "currency_field_id": "contracts.contract_header.currency"},
        "resolved_slots": {"metric": {"value": "signed_contract_amount", "resolution": "certified_definition", "evidence_id": "m:v1"},
                           "buyer_city": {"value": "buyer_city", "resolution": "user_confirmed", "evidence_id": "turn:2"}},
        "ambiguity_status": "resolved", "limit": 100,
    })
    res = executor.execute(plan)
    assert res["ok"] is True
    assert res["columns"] == ["metric_0", "dim_0", "currency"]
    assert res["sql"].startswith("SELECT")
    # SUM applies to SIGNED headers only (drafts excluded), grouped by city+currency.
    assert res["row_count"] > 0
    hk_hkd = next((r[0] for r in res["rows"] if r[1] == "香港" and r[2] == "HKD"), None)
    assert hk_hkd is not None and hk_hkd > 0


def test_executor_point_lookup_returns_exact_row(executor):
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts", "query_mode": "point_lookup",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [], "metrics": [], "dimensions": [],
        "filters": [{"field_id": "contracts.contract_header.contract_number", "op": "eq", "value": "SK-0001"}],
        "time": None,
        "currency_policy": {"mode": "single_currency", "currency_field_id": "contracts.contract_header.currency", "currency_value": "HKD"},
        "resolved_slots": {}, "ambiguity_status": "not_applicable", "limit": 1,
    })
    res = executor.execute(plan)
    assert res["ok"] is True
    assert res["sql"].startswith("SELECT")
    assert res["row_count"] == 1
    assert res["rows"][0][res["columns"].index("contract_number")] == "SK-0001"


def test_executor_connects_read_only(executor):
    # No mutation is possible: the rendered SQL is SELECT-only and the connection
    # is read-only. A mutation attempt in the plan shape would be an SQL injection
    # probe; the renderer never accepts non-SELECT shapes.
    plan = QueryPlan.from_json({
        "version": 1, "source_id": "contracts", "query_mode": "point_lookup",
        "fact": {"model_id": "contracts.contract_header", "grain": "one row per contract"},
        "joins": [], "metrics": [], "dimensions": [],
        "filters": [{"field_id": "contracts.contract_header.contract_number", "op": "eq", "value": "SK-0001"}],
        "time": None,
        "currency_policy": {"mode": "single_currency", "currency_field_id": "contracts.contract_header.currency", "currency_value": "HKD"},
        "resolved_slots": {}, "ambiguity_status": "not_applicable", "limit": 1,
    })
    res = executor.execute(plan)
    assert res["ok"] is True
    assert res["sql"].lstrip().upper().startswith("SELECT")
    assert not isinstance(res.get("rows"), str)
