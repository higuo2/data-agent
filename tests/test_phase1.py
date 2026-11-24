from __future__ import annotations
import json
import copy
from pathlib import Path
import pytest
from enterprise_data_agent.ambiguity import AmbiguityDetector
from enterprise_data_agent.catalog import Catalog
from enterprise_data_agent.linking import ExactEntityLinker
from enterprise_data_agent.preflight import Preflight
from enterprise_data_agent.retrieval import SemanticRetriever
from enterprise_data_agent.routing import IntentRouter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "enterprise-data-agent-fixture"

@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load(FIXTURE / "benchmark/catalog.json", FIXTURE / "benchmark/semantic_model.json")

@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))

def test_catalog_loads_four_systems_and_24_tables(catalog):
    assert len(catalog.systems) == 4
    assert len(catalog.tables) == 24
    assert {t.system_id for t in catalog.tables} == {"crm", "contracts", "sales", "finance"}

def test_catalog_has_foreign_key_metadata(catalog):
    table = next(t for t in catalog.tables if t.ref == "sales.order_items")
    assert any(fk["column"] == "order_id" and fk["references_table"] == "orders" for fk in table.foreign_keys)

def test_catalog_rejects_database_path_escape(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "testdata/enterprise-data-agent-fixture"
    raw = json.loads((fixture / "benchmark/catalog.json").read_text(encoding="utf-8"))
    raw["systems"][0]["database_file"] = "../../../../etc/passwd"
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes fixture root"):
        Catalog.load(path, fixture / "benchmark/semantic_model.json")

def test_router_classifies_greeting(config):
    route = IntentRouter(**_router_args(config)).classify("你好")
    assert route.intent == "smalltalk"

def test_router_sends_weather_to_unsupported_without_tool(config):
    route = IntentRouter(**_router_args(config)).classify("香港天气怎么样")
    assert route.intent == "unsupported"
    assert route.tool is None

def test_router_routes_weather_when_tool_registered(config):
    args = _router_args(config)
    args["supported_external_tools"] = ["weather"]
    route = IntentRouter(**args).classify("今天香港天气如何")
    assert route.intent == "external_tool"
    assert route.tool == "weather"

def test_router_detects_data_question(config):
    route = IntentRouter(**_router_args(config)).classify("SK-0001 的含税合同金额是多少？")
    assert route.intent == "data_query"

def test_router_blocks_mutation_request(config):
    route = IntentRouter(**_router_args(config)).classify("删除合同 SK-0001")
    assert route.intent == "unsafe"

def test_retriever_ranks_contract_header_for_signed_amount(catalog):
    candidates = SemanticRetriever(catalog, _retrieval_settings()).search("去年已签合同金额是多少", limit=8)
    refs = [c.ref for c in candidates]
    assert "contracts.contract_header" in refs
    assert refs.index("contracts.contract_header") < refs.index("contracts.contract_version") if "contracts.contract_version" in refs else True

def test_retriever_can_return_no_candidate_above_threshold(catalog):
    settings = _retrieval_settings(); settings["minimum_lexical_score"] = 0.1
    candidates = SemanticRetriever(catalog, settings).search("quuxzzzz")
    assert candidates == []

def test_identifier_alone_boosts_bound_contract_table(catalog):
    candidates = SemanticRetriever(catalog, _retrieval_settings()).search("查一下 SK-0001 的状态", limit=3)
    assert candidates[0].ref == "contracts.contract_header"
    assert "identifier pattern target" in candidates[0].reason

def test_retrieval_boosts_are_configurable(catalog):
    settings = _retrieval_settings()
    settings["minimum_lexical_score"] = 0.0
    default_score = next(c.score for c in SemanticRetriever(catalog, settings).search("查一下 SK-0001 的状态", 8) if c.ref == "contracts.contract_header")
    settings["identifier_target_boost"] = 0
    no_identifier_boost_score = next(c.score for c in SemanticRetriever(catalog, settings).search("查一下 SK-0001 的状态", 8) if c.ref == "contracts.contract_header")
    assert default_score - no_identifier_boost_score == 1.0

def test_retriever_uses_chinese_character_ngrams(catalog):
    candidates = SemanticRetriever(catalog, _retrieval_settings()).search("香港地区的已签合同金额是多少", limit=8)
    assert "contracts.contract_header" in [c.ref for c in candidates]

def test_exact_linker_finds_contract_identifier(catalog):
    refs = {"contracts.contract_header", "contracts.contract_version"}
    matches = ExactEntityLinker(catalog).link("SK-0001 的合同金额", refs)
    assert any(m.value == "SK-0001" and m.table == "contracts.contract_header" and m.column == "contract_number" for m in matches)

def test_exact_linker_respects_candidate_allowlist(catalog):
    matches = ExactEntityLinker(catalog).link("SK-0001 的合同金额", {"sales.orders"})
    assert matches == []

def test_exact_linker_parameterizes_injected_suffix(catalog):
    matches = ExactEntityLinker(catalog).link("SK-0001' OR 1=1 --", {"contracts.contract_header"})
    assert all(m.rows == 1 for m in matches)
    assert len(matches) == 1

def test_ambiguity_detector_flags_shared_city_without_role(catalog):
    ambiguity = AmbiguityDetector(catalog.ontology).detect("香港地区的已签合同金额是多少")
    assert ambiguity is not None
    assert ambiguity["kind"] == "dimension_role"
    assert "buyer_city" in ambiguity["options"] and "seller_city" in ambiguity["options"]

def test_ambiguity_detector_accepts_explicit_buyer_role(catalog):
    ambiguity = AmbiguityDetector(catalog.ontology).detect("香港买方城市的已签合同金额是多少")
    assert ambiguity is None

def test_ambiguity_detector_flags_generic_revenue_metric(catalog):
    ambiguity = AmbiguityDetector(catalog.ontology).detect("各客户收入最高的是谁")
    assert ambiguity is not None
    assert ambiguity["kind"] == "metric"

def test_metric_disambiguation_cues_are_ontology_configurable(catalog):
    ontology = copy.deepcopy(catalog.ontology)
    revenue_case = next(c for c in ontology["ambiguity_cases"] if c.get("candidate_metrics") and c.get("term", "").startswith("收入"))
    revenue_case["disambiguating_terms"] = ["指定的新口径词"]
    ambiguity = AmbiguityDetector(ontology).detect("各客户收入最高的是谁")
    assert ambiguity is not None and ambiguity["kind"] == "metric"

def test_ambiguity_detector_accepts_explicit_tax_included_contract_metric(catalog):
    ambiguity = AmbiguityDetector(catalog.ontology).detect("SK-0001 的含税合同金额是多少")
    assert ambiguity is None

def test_preflight_skips_database_for_smalltalk(catalog, config):
    result = Preflight(catalog, config).inspect("你好")
    assert result["decision"] == "smalltalk"
    assert result["candidates"]["tables"] == []
    assert result["entity_matches"] == []

def test_preflight_returns_clarification_for_city_role(catalog, config):
    result = Preflight(catalog, config).inspect("香港地区的已签合同金额是多少")
    assert result["decision"] == "clarify"
    assert result["clarification"]["kind"] == "dimension_role"

def test_preflight_links_exact_contract_number(catalog, config):
    result = Preflight(catalog, config).inspect("SK-0001 的含税合同金额是多少")
    assert result["decision"] == "retrieval_ready"
    assert any(m["value"] == "SK-0001" for m in result["entity_matches"])

def test_preflight_routes_identifier_status_question_and_finds_contract(catalog, config):
    result = Preflight(catalog, config).inspect("查一下 SK-0001 的状态")
    assert result["decision"] == "retrieval_ready"
    assert result["candidate_tables"][0]["table"] == "contracts.contract_header"
    assert result["resolved_entities"][0]["value"] == "SK-0001"

def test_preflight_exposes_phase2_retrieval_contract(catalog, config):
    result = Preflight(catalog, config).inspect("SK-0001 的签约金额是多少")
    assert result["decision"] == "retrieval_ready"
    assert result["intent"] == "data_query"
    assert result["selected_system"] == "contracts"
    assert result["candidate_tables"][0]["table"] == "contracts.contract_header"
    assert result["candidate_tables"][0]["grain"] == "one row per contract"
    assert result["resolved_metric"]["canonical"] == "signed_contract_amount"
    assert result["resolved_metric"]["aggregation"] == "sum"
    assert result["resolved_entities"][0]["column"] == "contract_number"
    assert result["resolved_entities"][0]["match_type"] == "exact"

def _router_args(config):
    return {
        "supported_external_tools": config["routing"].get("supported_external_tools", []),
        "mutation_verbs": config["routing"].get("mutation_verbs", []),
        "greetings": config["routing"].get("greetings", []),
        "external_tool_signals": config["routing"].get("external_tool_signals", {}),
        "data_signals": config["routing"].get("data_signals", []),
        "confidence": config["routing"].get("confidence", {}),
    }

def _retrieval_settings():
    return json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))["retrieval"]
