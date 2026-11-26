# 规划器：把预检结果收成有界查询计划。歧义未解或形状超出本阶段时拒绝执行。
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from .catalog import Catalog, Table
from .executor import QueryExecutor
from .preflight import Preflight
from .queryplan import QueryPlan, QueryPlanValidation, QueryPlanValidator, parse_declared_filter
from .slots import (
    SessionState,
    SlotProposal,
    clarification_kind_key,
    extract_slots,
    is_abandon,
    merge_clarification_answer,
)

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
    session: SessionState | None = None
    slots: SlotProposal | None = None

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
            "slots": self.slots.to_json() if self.slots else None,
            "session": self.session.to_json() if self.session else None,
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
        self.metrics_by_id = {str(m.get("id") or ""): m for m in catalog.ontology.get("metrics", []) if m.get("id")}
        self.dimensions_by_id = {str(d.get("id") or ""): d for d in catalog.ontology.get("dimensions", []) if d.get("id")}

    def plan(
        self,
        question: str,
        execute: bool = False,
        session: SessionState | None = None,
        slots: SlotProposal | dict[str, Any] | None = None,
    ) -> PlanningResult:
        if session is None:
            session = SessionState()
        if session.closed:
            return PlanningResult(question, {}, "closed", "session closed", session=session)

        if session.pending_clarification:
            result = self._resume(question, session)
            if result is not None:
                return result
            question = session.working_question()
        else:
            session.original_question = question
            session.answers = []
            session.resolved_choices = {}
            session.partial_plan = None

        return self._plan_question(question, session, execute, slots)

    def _resume(self, answer: str, session: SessionState) -> PlanningResult | None:
        if is_abandon(answer, self.config):
            session.closed = True
            session.pending_clarification = None
            return PlanningResult(answer, {}, "closed", "clarification abandoned", session=session)
        chosen = merge_clarification_answer(answer, session.pending_clarification or {}, self.catalog.ontology)
        if chosen is None:
            inspected = self.preflight.inspect(session.working_question() or session.original_question)
            plan = self._unresolved_plan(session.original_question, inspected)
            validation = self.validator.validate(plan) if plan else None
            return PlanningResult(
                answer, inspected, "clarify",
                str((session.pending_clarification or {}).get("question") or "clarification required"),
                plan, validation, False, None, session,
            )
        session.resolved_choices[clarification_kind_key(session.pending_clarification or {})] = chosen
        session.answers.append(answer)
        session.pending_clarification = None
        return None

    def _plan_question(
        self,
        question: str,
        session: SessionState,
        execute: bool,
        slots: SlotProposal | dict[str, Any] | None,
    ) -> PlanningResult:
        inspected = self.preflight.inspect(question)
        route_intent = str((inspected.get("route") or {}).get("intent") or inspected.get("decision") or "")
        if route_intent not in {"data_query", "data_exploration"}:
            return PlanningResult(
                question, inspected, route_intent or "refused", route_intent or "refused",
                session=session,
            )
        proposal = self._proposal(question, slots)
        proposal = self._apply_session_slots(proposal, session)
        clarification = inspected.get("clarification")
        if clarification and not self._clarification_satisfied(clarification, session, proposal):
            plan = self._unresolved_plan(question, inspected, proposal)
            session.pending_clarification = clarification
            session.partial_plan = plan.to_json() if plan else None
            validation = self.validator.validate(plan) if plan else None
            return PlanningResult(
                question, inspected, "clarify",
                str(clarification.get("question") or "clarification required"),
                plan, validation, False, None, session, proposal,
            )
        if proposal.unmatched_constraints:
            return PlanningResult(
                question, inspected, "refused",
                "unmatched_constraint:" + ",".join(proposal.unmatched_constraints),
                session=session, slots=proposal,
            )
        built, reason = self._build(question, inspected, proposal)
        if built is None:
            return PlanningResult(question, inspected, "refused", reason, session=session, slots=proposal)
        validation = self.validator.validate(built)
        execution = None
        if validation.valid and execute:
            execution = self.executor.execute(built)
        return PlanningResult(
            question, inspected,
            "execute" if validation.valid else "refused",
            "ready" if validation.valid else ",".join(validation.reasons),
            built, validation, validation.valid, execution, session, proposal,
        )

    # -- plan construction -------------------------------------------------

    def _proposal(self, question: str, slots: SlotProposal | dict[str, Any] | None) -> SlotProposal:
        lexical = extract_slots(question, self.catalog.ontology, self.config)
        if slots is None:
            return lexical
        proposal = slots if isinstance(slots, SlotProposal) else SlotProposal.from_json(slots)
        unmatched = tuple(dict.fromkeys([*lexical.unmatched_constraints, *proposal.unmatched_constraints]))
        extracted = tuple(dict.fromkeys([*lexical.extracted_constraints, *proposal.extracted_constraints]))
        return SlotProposal(
            metric_ids=proposal.metric_ids,
            dimension_ids=proposal.dimension_ids,
            time_phrases=proposal.time_phrases or lexical.time_phrases,
            currency_codes=proposal.currency_codes or lexical.currency_codes,
            extracted_constraints=extracted,
            unmatched_constraints=unmatched,
            source=proposal.source,
            time_bucket_grain=proposal.time_bucket_grain or lexical.time_bucket_grain,
            comparisons=proposal.comparisons or lexical.comparisons,
            in_values=proposal.in_values or lexical.in_values,
            top_n=proposal.top_n if proposal.top_n is not None else lexical.top_n,
        )

    def _apply_session_slots(self, slots: SlotProposal, session: SessionState) -> SlotProposal:
        metric_ids = list(slots.metric_ids)
        dimension_ids = list(slots.dimension_ids)
        chosen_metric = session.resolved_choices.get("metric")
        if chosen_metric:
            metric_ids = [chosen_metric]
        chosen_dim = session.resolved_choices.get("dimension_role")
        if chosen_dim and chosen_dim not in dimension_ids:
            dimension_ids.append(chosen_dim)
        return SlotProposal(
            metric_ids=tuple(metric_ids),
            dimension_ids=tuple(dimension_ids),
            time_phrases=slots.time_phrases,
            currency_codes=slots.currency_codes,
            extracted_constraints=slots.extracted_constraints,
            unmatched_constraints=slots.unmatched_constraints,
            source=slots.source,
            time_bucket_grain=slots.time_bucket_grain,
            comparisons=slots.comparisons,
            in_values=slots.in_values,
            top_n=slots.top_n,
        )

    def _clarification_satisfied(
        self,
        clarification: dict[str, Any],
        session: SessionState,
        slots: SlotProposal,
    ) -> bool:
        kind = clarification_kind_key(clarification)
        options = {str(item) for item in clarification.get("options") or []}
        if kind == "dimension_role":
            chosen = session.resolved_choices.get(kind)
            return bool(chosen and chosen in options) or any(item in options for item in slots.dimension_ids)
        if kind == "metric":
            chosen = session.resolved_choices.get(kind)
            return bool(chosen and chosen in options) or (
                len(slots.metric_ids) == 1 and slots.metric_ids[0] in options
            )
        return False

    def _build(
        self,
        question: str,
        inspected: dict[str, Any],
        slots: SlotProposal,
    ) -> tuple[QueryPlan | None, str]:
        metric = None
        if len(slots.metric_ids) == 1:
            metric = self.metrics_by_id.get(slots.metric_ids[0])
            if metric is None:
                return None, "metric_not_certified"
        elif len(slots.metric_ids) > 1:
            return None, "unresolved_ambiguity"
        if metric is None:
            metric = self._metric_from_candidates(inspected, slots)
        if metric is not None:
            if metric.get("composition"):
                return self._composite_plan(question, inspected, metric, slots)
            return self._aggregate_plan(question, inspected, metric, slots)
        return self._point_lookup_plan(inspected)

    def _aggregate_plan(
        self,
        question: str,
        inspected: dict[str, Any],
        metric: dict[str, Any],
        slots: SlotProposal,
    ) -> tuple[QueryPlan | None, str]:
        fact_ref = str(metric.get("model") or "")
        fact = self.tables.get(fact_ref)
        if fact is None:
            return None, "unknown_model"
        if metric.get("required_dimension") and not self.dimensions_by_id.get(str(metric.get("required_dimension"))):
            return None, "unsupported_metric_shape"
        measure = str(metric.get("measure") or "")
        if not any(c.name == measure for c in fact.columns):
            return None, "unsupported_metric_shape"
        filters, filter_reason = self._metric_filters(metric, fact)
        if filter_reason:
            return None, filter_reason
        snapshot, snapshot_reason = self._snapshot_filter(question, metric, fact)
        if snapshot_reason:
            return None, snapshot_reason
        if snapshot:
            filters.append(snapshot)
        regular_ids, dim_grain = self._regular_dimension_ids(slots.dimension_ids)
        dimensions, joins, dim_reason = self._dimensions_for_ids(regular_ids, fact_ref)
        if dim_reason:
            return None, dim_reason
        currency_field = str(metric.get("currency_field") or "")
        if currency_field:
            currency_table = currency_field.rpartition(".")[0]
            if currency_table and currency_table != fact_ref:
                extra, extra_reason = self._safe_join(fact_ref, currency_table, {})
                if extra_reason:
                    return None, extra_reason
                if extra and extra["relationship_id"] not in {j["relationship_id"] for j in joins}:
                    joins.append(extra)
        extra_tables = {str(j.get("dimension_model_id") or "") for j in joins}
        entity_filters, entity_joins, entity_reason = self._entity_filters(inspected, fact_ref, extra_tables)
        if entity_reason:
            return None, entity_reason
        for item in entity_joins:
            if item["relationship_id"] not in {j["relationship_id"] for j in joins}:
                joins.append(item)
        filters.extend(entity_filters)
        for item in slots.comparisons:
            filters.append({"field_id": f"{fact.ref}.{measure}", "op": item["op"], "value": item["value"]})
        if slots.in_values:
            if len(dimensions) != 1:
                return None, "unresolved_ambiguity"
            values = list(slots.in_values)
            if len(values) == 1:
                filters.append({"field_id": dimensions[0]["field_id"], "op": "eq", "value": values[0]})
            else:
                filters.append({"field_id": dimensions[0]["field_id"], "op": "in", "value": values})
        window, time_reason = self._time_window(question, fact_ref)
        if time_reason:
            return None, time_reason
        grain = slots.time_bucket_grain or dim_grain
        bucket = None
        if grain:
            bucket, bucket_reason = self._time_bucket(fact_ref, grain)
            if bucket_reason:
                return None, bucket_reason
        currency, currency_filter = self._currency(question, fact, currency_field or None)
        if currency_filter:
            filters.append(currency_filter)
        if currency is None and (
            any(c.name.casefold() == "currency" for c in fact.columns) or currency_field
        ):
            return None, "currency_policy_required"
        mapping, mapping_reason = self._entity_mapping(dimensions)
        if mapping_reason:
            return None, mapping_reason
        resolved = {
            "metric": {
                "value": str(metric.get("id") or ""),
                "resolution": "explicit_question",
                "evidence_id": f"metric:{metric.get('id')}",
            }
        }
        for dim in dimensions:
            resolved[dim["dimension_id"]] = {
                "value": dim["dimension_id"],
                "resolution": "explicit_question",
                "evidence_id": f"dimension:{dim['dimension_id']}",
            }
        limit = int(self.pcfg.get("default_limit", 100))
        order_by: list[dict[str, Any]] = []
        if slots.top_n and mapping is None:
            limit = int(slots.top_n)
            order_by = [{"target": "metric_0", "descending": True}]
        if mapping is not None and slots.top_n:
            mapping["top_n"] = int(slots.top_n)
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
            "resolved_slots": resolved,
            "ambiguity_status": "resolved",
            "limit": limit,
            "time_bucket": bucket,
            "order_by": order_by,
            "entity_mapping": mapping,
            "unique_key": str(metric["unique_key"]) if metric.get("unique_key") else None,
        })
        return plan, "ready"

    def _composite_plan(
        self,
        question: str,
        inspected: dict[str, Any],
        metric: dict[str, Any],
        slots: SlotProposal,
    ) -> tuple[QueryPlan | None, str]:
        spec = dict(metric.get("composition") or {})
        if str(spec.get("op") or "").casefold() != "subtract":
            return None, "unsupported_metric_shape"
        left_def = self.metrics_by_id.get(str(spec.get("minuend") or ""))
        right_def = self.metrics_by_id.get(str(spec.get("subtrahend") or ""))
        if left_def is None or right_def is None:
            return None, "metric_not_certified"
        left, left_reason = self._aggregate_plan(question, inspected, left_def, slots)
        if left is None:
            return None, left_reason
        right, right_reason = self._aggregate_plan(question, inspected, right_def, slots)
        if right is None:
            return None, right_reason
        return QueryPlan.from_json({
            "version": int(self.config.get("query_plan", {}).get("schema_version", 1)),
            "source_id": left.source_id,
            "query_mode": "composite",
            "fact": left.fact,
            "joins": [],
            "metrics": [],
            "dimensions": [],
            "filters": [],
            "time": None,
            "currency_policy": None,
            "resolved_slots": {
                "metric": {
                    "value": str(metric.get("id") or ""),
                    "resolution": "explicit_question",
                    "evidence_id": f"metric:{metric.get('id')}",
                }
            },
            "ambiguity_status": "resolved",
            "limit": left.limit,
            "composition": {
                "op": "subtract",
                "minuend": left.to_json(),
                "subtrahend": right.to_json(),
            },
        }), "ready"

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

    def _unresolved_plan(
        self,
        question: str,
        inspected: dict[str, Any],
        slots: SlotProposal | None = None,
    ) -> QueryPlan | None:
        clarification = inspected.get("clarification") or {}
        metric = None
        if clarification.get("kind") != "metric":
            if slots and len(slots.metric_ids) == 1:
                metric = self.metrics_by_id.get(slots.metric_ids[0])
            else:
                metric = self._unique_metric(question)
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

    def _regular_dimension_ids(self, dimension_ids: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
        regular: list[str] = []
        grain = None
        for ident in dimension_ids:
            dim = self.dimensions_by_id.get(ident)
            if dim and dim.get("time_role"):
                declared = str(dim.get("bucket_grain") or "").casefold()
                if declared:
                    grain = declared
                continue
            if dim and dim.get("snapshot_role"):
                continue
            regular.append(ident)
        return tuple(regular), grain

    def _time_bucket(self, fact_ref: str, grain: str) -> tuple[dict[str, Any] | None, str | None]:
        allowed = {str(g).casefold() for g in self.config.get("query_plan", {}).get("time_bucket_grains", [])}
        if allowed and grain.casefold() not in allowed:
            return None, "time_policy_required"
        fields: list[str] = []
        for dim in self.catalog.ontology.get("dimensions", []):
            if not dim.get("time_role"):
                continue
            field = str(dim.get("field") or "")
            if field.rpartition(".")[0] == fact_ref and field not in fields:
                fields.append(field)
        if len(fields) != 1:
            return None, "time_policy_required"
        return {"field_id": fields[0], "grain": grain.casefold()}, None

    def _metric_from_candidates(self, inspected: dict[str, Any], slots: SlotProposal) -> dict[str, Any] | None:
        if not slots.comparisons:
            return None
        tables = inspected.get("candidate_tables") or []
        if not tables:
            return None
        fact_ref = str(tables[0].get("table") or "")
        matches = []
        fact = self.tables.get(fact_ref)
        if fact is None:
            return None
        for metric in self.catalog.ontology.get("metrics", []):
            if str(metric.get("model") or "") != fact_ref:
                continue
            measure = str(metric.get("measure") or "")
            if any(c.name == measure for c in fact.columns):
                matches.append(metric)
        return matches[0] if len(matches) == 1 else None

    def _unique_metric(self, question: str) -> dict[str, Any] | None:
        slots = extract_slots(question, self.catalog.ontology, self.config)
        if len(slots.metric_ids) != 1:
            return None
        return self.metrics_by_id.get(slots.metric_ids[0])

    def _metric_filters(self, metric: dict[str, Any], fact: Table) -> tuple[list[dict[str, Any]], str | None]:
        filters = []
        for raw in metric.get("filters") or []:
            parsed = parse_declared_filter(str(raw).strip())
            if parsed is None:
                return [], "unsupported_metric_filter"
            column = str(parsed["column"])
            if not any(c.name == column for c in fact.columns):
                return [], "unsupported_metric_filter"
            filters.append({"field_id": f"{fact.ref}.{column}", "op": parsed["op"], "value": parsed["value"]})
        return filters, None

    def _snapshot_filter(
        self,
        question: str,
        metric: dict[str, Any],
        fact: Table,
    ) -> tuple[dict[str, Any] | None, str | None]:
        required = str(metric.get("required_dimension") or "")
        if not required:
            return None, None
        dim = self.dimensions_by_id.get(required)
        if dim is None or not dim.get("snapshot_role"):
            return None, "unsupported_metric_shape"
        field = str(dim.get("field") or "")
        if field.rpartition(".")[0] != fact.ref:
            return None, "time_policy_required"
        pattern = str(self.pcfg.get("snapshot_date_regex") or "")
        if not pattern:
            return None, "time_policy_required"
        dates = list(dict.fromkeys(re.findall(pattern, question or "")))
        if len(dates) != 1:
            return None, "time_policy_required"
        return {"field_id": field, "op": "eq", "value": dates[0]}, None

    def _entity_mapping(
        self,
        dimensions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str | None]:
        found: list[dict[str, Any]] = []
        for dim in dimensions:
            rec = self.dimensions_by_id.get(str(dim.get("dimension_id") or "")) or {}
            entity_id = rec.get("maps_to_entity")
            if not entity_id:
                continue
            entity = next(
                (item for item in self.catalog.ontology.get("canonical_entities", [])
                 if str(item.get("id") or "") == str(entity_id)),
                None,
            )
            if entity is None or not entity.get("mapping_table") or not entity.get("validity_required"):
                return None, "dimension_not_reachable"
            found.append({
                "entity_id": str(entity_id),
                "source_field_id": dim["field_id"],
                "as_of": str(self.pcfg.get("as_of_date") or ""),
            })
        if len(found) > 1:
            return None, "unresolved_ambiguity"
        return (found[0] if found else None), None

    def _entity_filters(
        self,
        inspected: dict[str, Any],
        fact_ref: str,
        extra_tables: set[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        entities = [e for e in inspected.get("resolved_entities") or [] if int(e.get("rows") or 0) >= 1]
        filters: list[dict[str, Any]] = []
        joins: list[dict[str, Any]] = []
        allowed = {fact_ref, *extra_tables}
        for entity in entities:
            table = str(entity.get("table") or "")
            if table not in allowed:
                join, reason = self._safe_join(fact_ref, table, {})
                if reason or join is None:
                    return [], [], reason or "entity_not_on_fact"
                joins.append(join)
                allowed.add(table)
            filters.append({
                "field_id": f"{table}.{entity['column']}",
                "op": "eq",
                "value": entity["value"],
            })
        return filters, joins, None

    def _dimensions_for_ids(
        self,
        dimension_ids: tuple[str, ...],
        fact_ref: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        matched = []
        seen: set[str] = set()
        for ident in dimension_ids:
            dim = self.dimensions_by_id.get(ident)
            if dim is None:
                return [], [], "unknown_field"
            if ident in seen:
                continue
            seen.add(ident)
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

    def _currency(
        self,
        question: str,
        fact: Table,
        currency_field: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        field_id = currency_field if currency_field else (
            f"{fact.ref}.currency" if any(c.name.casefold() == "currency" for c in fact.columns) else ""
        )
        if not field_id:
            return None, None
        q = (question or "").casefold()
        hits = [m for m in self.pcfg.get("currency_mentions", []) if str(m.get("phrase") or "").casefold() in q]
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
