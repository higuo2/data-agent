# 规划器：把预检结果收成有界查询计划。歧义未解或形状超出本阶段时拒绝执行。
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from .catalog import Catalog, Table
from .executor import QueryExecutor
from .preflight import Preflight
from .queryplan import QueryPlan, QueryPlanValidation, QueryPlanValidator

_EQ_FILTER = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']*)'\s*$")


@dataclass
class PlanningResult:
    question: str
    preflight: dict[str, Any]
    decision: str
    reason: str
    plan: QueryPlan | None = None
    validation: QueryPlanValidation | None = None
    executable: bool = False
    execution: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        validation = None
        if self.validation is not None:
            validation = {
                "valid": self.validation.valid,
                "issues": [
                    {"issue_type": i.issue_type, "path": i.path, "detail": i.detail}
                    for i in self.validation.issues
                ],
            }
        execution = None
        if self.execution is not None:
            execution = {
                "ok": self.execution.get("ok"),
                "issues": self.execution.get("issues", []),
                "columns": self.execution.get("columns", []),
                "rows": [list(row) for row in self.execution.get("rows", [])],
                "row_count": self.execution.get("row_count", 0),
                "sql": self.execution.get("sql"),
            }
        return {
            "question": self.question,
            "decision": self.decision,
            "reason": self.reason,
            "executable": self.executable,
            "preflight": self.preflight,
            "plan": self.plan.to_json() if self.plan else None,
            "validation": validation,
            "execution": execution,
        }


class QueryPlanner:
    """Turn a preflight handoff into one bounded QueryPlan, or refuse."""

    def __init__(self, catalog: Catalog, config: dict[str, Any] | None = None):
        self.catalog = catalog
        self.config = config or {}
        self.pcfg = self.config.get("planner", {})
        self.preflight = Preflight(catalog, self.config)
        self.validator = QueryPlanValidator(catalog, self.config)
        self.executor = QueryExecutor(catalog, self.config)
        self.tables = {t.ref: t for t in catalog.tables}

    def plan(self, question: str, execute: bool = False) -> PlanningResult:
        inspected = self.preflight.inspect(question)
        route_intent = str((inspected.get("route") or {}).get("intent") or inspected.get("decision") or "")
        if route_intent not in {"data_query", "data_exploration"}:
            return PlanningResult(question, inspected, route_intent or "refused", route_intent or "refused")
        if inspected.get("clarification"):
            plan = self._unresolved_plan(question, inspected)
            validation = self.validator.validate(plan) if plan else None
            return PlanningResult(
                question, inspected, "clarify",
                str(inspected["clarification"].get("question") or "clarification required"),
                plan, validation, False, None,
            )
        built, reason = self._build(question, inspected)
        if built is None:
            return PlanningResult(question, inspected, "refused", reason)
        validation = self.validator.validate(built)
        execution = None
        if validation.valid and execute:
            execution = self.executor.execute(built)
        return PlanningResult(
            question, inspected,
            "execute" if validation.valid else "refused",
            "ready" if validation.valid else ",".join(validation.reasons),
            built, validation, validation.valid, execution,
        )

    # -- plan construction -------------------------------------------------

    def _build(self, question: str, inspected: dict[str, Any]) -> tuple[QueryPlan | None, str]:
        metric = self._unique_metric(question)
        if metric is not None:
            return self._aggregate_plan(question, inspected, metric)
        return self._point_lookup_plan(inspected)

    def _aggregate_plan(self, question: str, inspected: dict[str, Any], metric: dict[str, Any]) -> tuple[QueryPlan | None, str]:
        fact_ref = str(metric.get("model") or "")
        fact = self.tables.get(fact_ref)
        if fact is None:
            return None, "unknown_model"
        if metric.get("required_dimension"):
            return None, "unsupported_metric_shape"
        measure = str(metric.get("measure") or "")
        if not any(c.name == measure for c in fact.columns):
            return None, "unsupported_metric_shape"
        filters, filter_reason = self._metric_filters(metric, fact)
        if filter_reason:
            return None, filter_reason
        entity_filters, entity_reason = self._entity_filters(inspected, fact_ref)
        if entity_reason:
            return None, entity_reason
        filters.extend(entity_filters)
        dimensions, joins, dim_reason = self._dimensions(question, fact_ref)
        if dim_reason:
            return None, dim_reason
        window, time_reason = self._time_window(question, fact_ref)
        if time_reason:
            return None, time_reason
        currency, currency_filter = self._currency(question, fact)
        if currency_filter:
            filters.append(currency_filter)
        if currency is None and any(c.name.casefold() == "currency" for c in fact.columns):
            return None, "currency_policy_required"
        slots = {
            "metric": {
                "value": str(metric.get("id") or ""),
                "resolution": "explicit_question",
                "evidence_id": f"metric:{metric.get('id')}",
            }
        }
        for dim in dimensions:
            slots[dim["dimension_id"]] = {
                "value": dim["dimension_id"],
                "resolution": "explicit_question",
                "evidence_id": f"dimension:{dim['dimension_id']}",
            }
        plan = QueryPlan.from_json({
            "version": int(self.config.get("query_plan", {}).get("schema_version", 1)),
            "source_id": fact.system_id,
            "query_mode": "aggregate",
            "fact": {"model_id": fact.ref, "grain": fact.grain},
            "joins": joins,
            "metrics": [{
                "metric_id": str(metric.get("id") or ""),
                "aggregate": str(metric.get("aggregation") or ""),
                "field_id": f"{fact.ref}.{measure}",
            }],
            "dimensions": dimensions,
            "filters": filters,
            "time": window,
            "currency_policy": currency,
            "resolved_slots": slots,
            "ambiguity_status": "resolved",
            "limit": int(self.pcfg.get("default_limit", 100)),
        })
        return plan, "ready"

    def _point_lookup_plan(self, inspected: dict[str, Any]) -> tuple[QueryPlan | None, str]:
        entities = [e for e in inspected.get("resolved_entities") or [] if int(e.get("rows") or 0) >= 1]
        if len(entities) != 1:
            return None, "insufficient_evidence"
        entity = entities[0]
        fact = self.tables.get(str(entity.get("table") or ""))
        if fact is None or not self._is_exact_column(fact, str(entity.get("column") or "")):
            return None, "insufficient_evidence"
        currency, _ = self._currency("", fact)
        plan = QueryPlan.from_json({
            "version": int(self.config.get("query_plan", {}).get("schema_version", 1)),
            "source_id": fact.system_id,
            "query_mode": "point_lookup",
            "fact": {"model_id": fact.ref, "grain": fact.grain},
            "joins": [],
            "metrics": [],
            "dimensions": [],
            "filters": [{
                "field_id": f"{fact.ref}.{entity['column']}",
                "op": "eq",
                "value": entity["value"],
            }],
            "time": None,
            "currency_policy": currency,
            "resolved_slots": {},
            "ambiguity_status": "not_applicable",
            "limit": int(self.pcfg.get("point_lookup_limit", 1)),
        })
        return plan, "ready"

    def _unresolved_plan(self, question: str, inspected: dict[str, Any]) -> QueryPlan | None:
        clarification = inspected.get("clarification") or {}
        metric = None if clarification.get("kind") == "metric" else self._unique_metric(question)
        fact = self.tables.get(str(metric.get("model") or "")) if metric else None
        if fact is None:
            candidates = inspected.get("candidate_tables") or []
            fact = self.tables.get(str(candidates[0]["table"])) if candidates else None
        if fact is None:
            return None
        metrics = []
        slots: dict[str, Any] = {}
        if metric and any(c.name == str(metric.get("measure") or "") for c in fact.columns):
            metrics.append({
                "metric_id": str(metric.get("id") or ""),
                "aggregate": str(metric.get("aggregation") or ""),
                "field_id": f"{fact.ref}.{metric.get('measure')}",
            })
            slots["metric"] = {
                "value": str(metric.get("id") or ""),
                "resolution": "explicit_question",
                "evidence_id": f"metric:{metric.get('id')}",
            }
        currency, _ = self._currency("", fact)
        return QueryPlan.from_json({
            "version": int(self.config.get("query_plan", {}).get("schema_version", 1)),
            "source_id": fact.system_id,
            "query_mode": "aggregate",
            "fact": {"model_id": fact.ref, "grain": fact.grain},
            "joins": [],
            "metrics": metrics,
            "dimensions": [],
            "filters": [],
            "time": None,
            "currency_policy": currency,
            "resolved_slots": slots,
            "ambiguity_status": "unresolved",
            "limit": int(self.pcfg.get("default_limit", 100)),
        })

    # -- ontology lookups --------------------------------------------------

    def _unique_metric(self, question: str) -> dict[str, Any] | None:
        q = (question or "").casefold()
        matches = []
        for metric in self.catalog.ontology.get("metrics", []):
            aliases = [str(a) for a in metric.get("aliases", []) if a]
            if any(alias.casefold() in q for alias in aliases):
                matches.append(metric)
        if len(matches) != 1:
            return None
        return matches[0]

    def _metric_filters(self, metric: dict[str, Any], fact: Table) -> tuple[list[dict[str, Any]], str | None]:
        filters = []
        for raw in metric.get("filters") or []:
            match = _EQ_FILTER.fullmatch(str(raw).strip())
            if not match:
                return [], "unsupported_metric_filter"
            column, value = match.group(1), match.group(2)
            if not any(c.name == column for c in fact.columns):
                return [], "unsupported_metric_filter"
            filters.append({"field_id": f"{fact.ref}.{column}", "op": "eq", "value": value})
        return filters, None

    def _entity_filters(self, inspected: dict[str, Any], fact_ref: str) -> tuple[list[dict[str, Any]], str | None]:
        entities = [e for e in inspected.get("resolved_entities") or [] if int(e.get("rows") or 0) >= 1]
        if any(str(e.get("table") or "") != fact_ref for e in entities):
            return [], "entity_not_on_fact"
        return [
            {"field_id": f"{fact_ref}.{e['column']}", "op": "eq", "value": e["value"]}
            for e in entities
        ], None

    def _dimensions(self, question: str, fact_ref: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        q = (question or "").casefold()
        matched = []
        for dim in self.catalog.ontology.get("dimensions", []):
            aliases = [str(a) for a in dim.get("aliases", []) if a]
            if any(alias.casefold() in q for alias in aliases):
                matched.append(dim)
        if any(dim.get("time_role") for dim in matched):
            return [], [], "unsupported_time_bucket"
        dimensions: list[dict[str, Any]] = []
        joins: list[dict[str, Any]] = []
        joined: set[str] = set()
        for dim in matched:
            field = str(dim.get("field") or "")
            table_ref = field.rpartition(".")[0]
            if table_ref == fact_ref:
                dimensions.append({"dimension_id": str(dim.get("id") or ""), "field_id": field})
                continue
            join, reason = self._safe_join(fact_ref, table_ref, dim)
            if reason:
                return [], [], reason
            if join["relationship_id"] in joined:
                existing = next(item for item in joins if item["relationship_id"] == join["relationship_id"])
                if existing.get("role_filter") != join.get("role_filter"):
                    return [], [], "fanout_risk"
            else:
                joins.append(join)
                joined.add(join["relationship_id"])
            dimensions.append({"dimension_id": str(dim.get("id") or ""), "field_id": field})
        return dimensions, joins, None

    def _safe_join(self, fact_ref: str, dimension_ref: str, dim: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        approved = {str(s) for s in self.pcfg.get("approved_relationship_statuses", ["known"])}
        chosen = None
        for rel in self.catalog.ontology.get("relationships", []):
            source = str(rel.get("from") or "").rpartition(".")[0]
            target = str(rel.get("to") or "").rpartition(".")[0]
            if source == fact_ref and target == dimension_ref:
                chosen = rel
                break
        if chosen is None:
            return None, "dimension_not_reachable"
        if str(chosen.get("status") or "") not in approved or chosen.get("fanout_warning"):
            return None, "fanout_risk"
        cardinality = str(chosen.get("cardinality") or "").casefold()
        join = {
            "relationship_id": str(chosen.get("id") or ""),
            "dimension_model_id": dimension_ref,
            "cardinality_from_fact": cardinality,
        }
        if cardinality in {"one_to_one", "many_to_one"}:
            return join, None
        if cardinality != "one_to_many" or not chosen.get("role_filter_required"):
            return None, "fanout_risk"
        parsed = _EQ_FILTER.fullmatch(str(dim.get("role_filter") or "").strip())
        if not parsed:
            return None, "fanout_risk"
        column, value = parsed.group(1), parsed.group(2)
        if not value or "," in value:
            return None, "fanout_risk"
        join["role_filter"] = {
            "field_id": f"{dimension_ref}.{column}",
            "op": str(self.config.get("query_plan", {}).get("role_filter_op", "eq")),
            "value": value,
        }
        return join, None

    def _time_window(self, question: str, fact_ref: str) -> tuple[dict[str, Any] | None, str | None]:
        q = (question or "").casefold()
        hits = [p for p in self.pcfg.get("time_phrases", []) if str(p.get("phrase") or "").casefold() in q]
        if not hits:
            return None, None
        if len(hits) != 1:
            return None, "time_policy_required"
        try:
            year = date.fromisoformat(str(self.pcfg.get("as_of_date") or "")).year + int(hits[0].get("year_offset"))
        except (TypeError, ValueError):
            return None, "time_policy_required"
        tz = str(self.pcfg.get("default_timezone") or "")
        allowed = {str(item) for item in self.config.get("query_plan", {}).get("timezones_allowed", [])}
        if not tz or (allowed and tz not in allowed):
            return None, "time_policy_required"
        fields = []
        for dim in self.catalog.ontology.get("dimensions", []):
            if not dim.get("time_role"):
                continue
            field = str(dim.get("field") or "")
            if field.rpartition(".")[0] == fact_ref and field not in fields:
                fields.append(field)
        if len(fields) != 1:
            return None, "time_policy_required"
        return {
            "field_id": fields[0],
            "start": f"{year:04d}-01-01",
            "end_exclusive": f"{year + 1:04d}-01-01",
            "timezone": tz,
        }, None

    def _currency(self, question: str, fact: Table) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not any(c.name.casefold() == "currency" for c in fact.columns):
            return None, None
        q = (question or "").casefold()
        hits = [m for m in self.pcfg.get("currency_mentions", []) if str(m.get("phrase") or "").casefold() in q]
        field_id = f"{fact.ref}.currency"
        if not hits:
            return {"mode": "group_by_currency", "currency_field_id": field_id}, None
        hits.sort(key=lambda item: len(str(item.get("phrase") or "")), reverse=True)
        codes = {str(item.get("code") or "") for item in hits if str(item.get("phrase") or "").casefold() in q and len(str(item.get("phrase") or "")) == len(str(hits[0].get("phrase") or ""))}
        code = str(hits[0].get("code") or "")
        if not code or len(codes) != 1:
            return None, None
        return (
            {"mode": "single_currency", "currency_field_id": field_id, "currency_value": code},
            {"field_id": field_id, "op": "eq", "value": code},
        )

    def _is_exact_column(self, fact: Table, column: str) -> bool:
        if any(c.name == column and (c.primary_key or c.name.casefold().endswith("_id")) for c in fact.columns):
            return True
        for item in self.catalog.ontology.get("identifier_patterns", []):
            table = str(item.get("table") or "")
            if "." not in table:
                table = f"{item.get('system_id', '')}.{table}".strip(".")
            if table == fact.ref and str(item.get("column") or "") == column:
                return True
        return False
