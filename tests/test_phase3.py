from __future__ import annotations
import json
from pathlib import Path
import pytest
from enterprise_data_agent.catalog import Catalog
from enterprise_data_agent.planner import QueryPlanner
from enterprise_data_agent.slots import SessionState, SlotProposal, extract_slots

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


def _questions() -> dict[str, dict]:
    rows = {}
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            rows[item["id"]] = item
    return rows


def test_slot_proposal_rejects_sql_and_joins():
    with pytest.raises(ValueError, match="cannot include"):
        SlotProposal.from_json({"metric_ids": ["signed_contract_amount"], "sql": "SELECT 1"})
    with pytest.raises(ValueError, match="cannot include"):
        SlotProposal.from_json({"metric_ids": ["signed_contract_amount"], "joins": []})
    with pytest.raises(ValueError, match="cannot include"):
        SlotProposal.from_json({"field_id": "contracts.contract_header.signed_amount_tax_included"})


def test_invalid_alias_match_fails_clearly(catalog, config):
    cfg = json.loads(json.dumps(config))
    cfg["slot_filling"]["alias_match"] = "embedding"
    with pytest.raises(ValueError, match="alias_match"):
        QueryPlanner(catalog, cfg).plan("已签合同金额是多少")


def test_q001_alias_variant_executes(planner):
    question = _questions()["Q001"]["question"]
    result = planner.plan(question, execute=True)
    assert result.decision == "execute"
    assert result.executable is True
    assert result.plan.metrics[0]["metric_id"] == "signed_contract_amount"
    assert result.execution["ok"] is True
    assert result.execution["sql"].lstrip().upper().startswith("SELECT")


def test_q003_clarifies_and_does_not_query(planner):
    question = _questions()["Q003"]["question"]
    result = planner.plan(question, execute=True)
    assert result.decision == "clarify"
    assert result.executable is False
    assert result.execution is None
    assert result.session is not None
    assert result.session.pending_clarification["kind"] == "dimension_role"
    assert result.session.partial_plan is not None


def test_q040_clarifies_and_does_not_query(planner):
    question = _questions()["Q040"]["question"]
    result = planner.plan(question, execute=True)
    assert result.decision == "clarify"
    assert result.executable is False
    assert result.execution is None
    assert result.session.pending_clarification["kind"] == "metric"


def test_city_role_merges_on_second_turn_and_executes(planner):
    session = SessionState()
    first = planner.plan(_questions()["Q003"]["question"], execute=True, session=session)
    assert first.decision == "clarify"
    assert first.execution is None
    second = planner.plan("买方", execute=True, session=session)
    assert second.executable is True
    assert second.plan.dimensions[0]["dimension_id"] == "buyer_city"
    assert second.plan.joins[0]["role_filter"]["value"] == "BUYER"
    assert second.execution["ok"] is True
    assert second.execution["row_count"] > 0
    assert second.session.pending_clarification is None


def test_unclear_follow_up_does_not_guess(planner):
    session = SessionState()
    planner.plan("香港地区的已签合同金额是多少", execute=True, session=session)
    again = planner.plan("随便", execute=True, session=session)
    assert again.decision == "clarify"
    assert again.executable is False
    assert again.execution is None
    assert session.pending_clarification is not None


def test_abandon_closes_without_query(planner):
    session = SessionState()
    planner.plan("香港地区的已签合同金额是多少", execute=True, session=session)
    closed = planner.plan("放弃", execute=True, session=session)
    assert closed.decision == "closed"
    assert closed.execution is None
    assert session.closed is True
    later = planner.plan("按买方城市统计已签合同金额", execute=True, session=session)
    assert later.decision == "closed"
    assert later.execution is None


def test_injected_slots_fill_dimension_without_writing_joins(planner):
    result = planner.plan(
        "已签合同金额是多少",
        execute=True,
        slots={"metric_ids": ["signed_contract_amount"], "dimension_ids": ["buyer_city"]},
    )
    assert result.executable is True
    assert result.slots.source == "injected"
    assert result.plan.dimensions[0]["dimension_id"] == "buyer_city"
    assert result.plan.joins[0]["relationship_id"] == "contract_to_party"
    assert "joins" not in result.slots.to_json()


def test_unmatched_window_constraint_is_refused(planner):
    result = planner.plan("已签合同金额同比是多少", execute=True)
    assert result.decision == "refused"
    assert result.reason == "unmatched_constraint:window"
    assert result.execution is None


def test_unmatched_constraint_signal_is_config_driven(catalog, config):
    question = "已签合同金额是多少"
    assert QueryPlanner(catalog, config).plan(question, execute=True).executable is True
    cfg = json.loads(json.dumps(config))
    cfg["slot_filling"]["unsupported_constraint_signals"] = [
        {"id": "forced_block", "phrases": ["已签合同金额"]}
    ]
    blocked = QueryPlanner(catalog, cfg).plan(question, execute=True)
    assert blocked.decision == "refused"
    assert blocked.reason == "unmatched_constraint:forced_block"
    assert blocked.execution is None


def test_follow_up_currency_enters_slots(planner):
    session = SessionState()
    planner.plan("香港地区的已签合同金额是多少", execute=True, session=session)
    result = planner.plan("买方，只要港币", execute=True, session=session)
    assert result.executable is True
    assert result.plan.currency_policy["mode"] == "single_currency"
    assert result.plan.currency_policy["currency_value"] == "HKD"
    assert "HKD" in result.slots.currency_codes


def test_benchmark_phase3_cases(planner):
    cases = _questions()
    q001 = planner.plan(cases["Q001"]["question"], execute=True)
    assert q001.executable is True
    q003 = planner.plan(cases["Q003"]["question"], execute=True)
    assert q003.decision == "clarify" and q003.execution is None
    q026 = planner.plan(cases["Q026"]["question"], execute=True)
    assert q026.decision == "smalltalk" and q026.execution is None
    q028 = planner.plan(cases["Q028"]["question"], execute=True)
    assert q028.decision == "unsafe" and q028.execution is None
    q040 = planner.plan(cases["Q040"]["question"], execute=True)
    assert q040.decision == "clarify" and q040.execution is None


def test_extract_slots_prefers_exact_alias(catalog, config):
    slots = extract_slots("SK-0001 的含税合同金额是多少", catalog.ontology, config)
    assert slots.metric_ids == ("signed_contract_amount",)
    assert slots.unmatched_constraints == ()
