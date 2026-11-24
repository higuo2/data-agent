# 预检流水线：先路由，再检索候选表、精确匹配实体、检测歧义，并整理成下一阶段可用的 JSON。
from __future__ import annotations
from typing import Any
from .ambiguity import AmbiguityDetector
from .catalog import Catalog
from .linking import ExactEntityLinker
from .retrieval import SemanticRetriever
from .routing import IntentRouter


class Preflight:
    def __init__(self, catalog: Catalog, config: dict[str, Any] | None = None):
        self.catalog = catalog
        cfg = config or {}
        retrieval = cfg.get("retrieval", {})
        routing = cfg.get("routing", {})
        sqlite_cfg = cfg.get("sqlite", {})
        self.retriever = SemanticRetriever(catalog, retrieval)
        self.router = IntentRouter(routing.get("supported_external_tools", []), routing.get("mutation_verbs", []), routing.get("greetings", []), routing.get("external_tool_signals", {}), routing.get("data_signals", []), routing.get("confidence", {}))
        self.linker = ExactEntityLinker(catalog, int(sqlite_cfg.get("max_identifier_matches", 10)))
        self.ambiguity = AmbiguityDetector(catalog.ontology)
        self.top_tables = int(retrieval.get("top_tables", 8))
        self.top_systems = int(retrieval.get("top_systems", 3))

    def inspect(self, question: str) -> dict[str, Any]:
        route = self.router.classify(question)
        result: dict[str, Any] = {"question": question, "route": route.__dict__, "decision": route.intent}
        if route.intent not in {"data_query", "data_exploration"}:
            result["candidates"] = {"systems": [], "tables": []}
            result["entity_matches"] = []
            result["clarification"] = None
            return result
        candidates = self.retriever.search(question, self.top_tables)
        result["candidates"] = {
            "systems": self.retriever.ranked_systems(candidates, self.top_systems),
            "tables": [c.__dict__ for c in candidates],
        }
        refs = {c.ref for c in candidates}
        entity_matches = self.linker.link(question, refs)
        result["entity_matches"] = [m.__dict__ for m in entity_matches]
        clarification = self.ambiguity.detect(question)
        result["clarification"] = clarification
        if clarification:
            result["decision"] = "clarify"
        elif not candidates:
            result["decision"] = "insufficient_catalog_evidence"
        else:
            result["decision"] = "retrieval_ready"
        result["intent"] = route.intent
        result["selected_system"] = candidates[0].system_id if candidates else None
        tables_by_ref = {t.ref: t for t in self.catalog.tables}
        result["candidate_tables"] = [
            {"table": c.ref, "score": c.score, "grain": tables_by_ref[c.ref].grain}
            for c in candidates
        ]
        result["resolved_metric"] = self._resolve_metric(question) if result["decision"] == "retrieval_ready" else None
        result["resolved_entities"] = [
            {"entity_type": m.entity_type, "table": m.table, "column": m.column, "value": m.value, "match_type": "exact", "rows": m.rows}
            for m in entity_matches
        ]
        return result

    def _resolve_metric(self, question: str) -> dict[str, Any] | None:
        q = (question or "").casefold()
        matches = []
        for metric in self.catalog.ontology.get("metrics", []):
            aliases = [str(a) for a in metric.get("aliases", [])]
            if any(alias.casefold() in q for alias in aliases if alias):
                matches.append(metric)
        if len(matches) != 1:
            return None
        metric = matches[0]
        model = str(metric.get("model") or "")
        measure = str(metric.get("measure") or "")
        table = next((t for t in self.catalog.tables if t.ref == model), None)
        physical_column = measure if table and any(c.name == measure for c in table.columns) else None
        return {
            "canonical": str(metric.get("id") or ""),
            "physical_column": f"{model}.{physical_column}" if physical_column else None,
            "model": model,
            "measure": measure,
            "aggregation": str(metric.get("aggregation") or ""),
            "grain": str(metric.get("grain") or ""),
            "filters": list(metric.get("filters") or []),
            "authority": metric.get("authority"),
        }
