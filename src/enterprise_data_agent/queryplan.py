"""Phase 2 QueryPlan data model and validator (first slice).

The compiler is bounded to proven plan shapes: one fact table plus catalog-approved
N:1 (or role-filtered / current-filtered 1:N) dimensions, typed WHERE/GROUP BY and
SUM/COUNT/AVG, one physical source per SQL statement. Phase 5 adds composite
subtract-of-two-plans and validity-window entity mapping as separate read-only
queries, not nested SQL. The validator recomputes source, field membership, metric
definition, grain, join cardinality, role/current filters, currency/time policy,
aggregate operation, and limits from the versioned catalog; it never trusts a
plan's self-declared cardinality claims.

Fan-out protection is the core invariant: a join that can multiply a measure
is rejected with a structured `fanout_risk` issue. Arbitrary nested SQL and free
CTEs are out of scope.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any
from .catalog import Catalog

_EQ_FILTER = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:'([^']*)'|(\d+))\s*$")
_IN_FILTER = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+IN\s*\((.*)\)\s*$", re.I)


def parse_declared_filter(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    matched = _EQ_FILTER.fullmatch(text)
    if matched:
        value: Any = matched.group(2) if matched.group(2) is not None else int(matched.group(3))
        return {"column": matched.group(1), "op": "eq", "value": value}
    matched = _IN_FILTER.fullmatch(text)
    if matched:
        values = re.findall(r"'([^']*)'", matched.group(2))
        if not values:
            return None
        return {"column": matched.group(1), "op": "in", "value": values}
    return None

# Issue vocabulary (protocol invariants from the Phase 2 contract).
ISSUE_UNKNOWN_MODEL = "unknown_model"
ISSUE_UNKNOWN_FIELD = "unknown_field"
ISSUE_METRIC_NOT_CERTIFIED = "metric_not_certified"
ISSUE_UNRESOLVED_AMBIGUITY = "unresolved_ambiguity"
ISSUE_GRAIN_MISMATCH = "grain_mismatch"
ISSUE_UNSUPPORTED_AGGREGATE = "unsupported_aggregate"
ISSUE_CROSS_SOURCE_PLAN = "cross_source_plan"
ISSUE_RELATIONSHIP_NOT_APPROVED = "relationship_not_approved"
ISSUE_FANOUT_RISK = "fanout_risk"
ISSUE_CURRENCY_POLICY_REQUIRED = "currency_policy_required"
ISSUE_TIME_POLICY_REQUIRED = "time_policy_required"
ISSUE_QUERY_BUDGET_EXCEEDED = "query_budget_exceeded"
# First-slice additions (not in the original contract outcome list): a plan
# with no metrics has nothing to execute (aggregate mode), and a point lookup
# without an exact entity pin has nothing exact to return.
ISSUE_EMPTY_METRIC_SET = "empty_metric_set"
ISSUE_UNSUPPORTED_QUERY_MODE = "unsupported_query_mode"
ISSUE_POINT_LOOKUP_REQUIRES_EXACT_FILTER = "point_lookup_requires_exact_filter"


@dataclass(frozen=True)
class PlanIssue:
    issue_type: str
    path: str
    detail: str


@dataclass(frozen=True)
class QueryPlanValidation:
    valid: bool
    issues: tuple[PlanIssue, ...]
    cross_source: bool = False
    fanout_risk: bool = False

    @property
    def reasons(self) -> list[str]:
        return [i.issue_type for i in self.issues]


@dataclass(frozen=True)
class QueryPlan:
    version: int
    source_id: str
    query_mode: str
    fact: dict[str, Any]
    joins: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    dimensions: tuple[dict[str, Any], ...]
    filters: tuple[dict[str, Any], ...]
    time: dict[str, Any] | None
    currency_policy: dict[str, Any] | None
    resolved_slots: dict[str, Any]
    ambiguity_status: str
    limit: int
    time_bucket: dict[str, Any] | None = None
    order_by: tuple[dict[str, Any], ...] = ()
    composition: dict[str, Any] | None = None
    entity_mapping: dict[str, Any] | None = None
    unique_key: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "QueryPlan":
        return cls(
            version=int(raw.get("version") or 0),
            source_id=str(raw.get("source_id") or ""),
            query_mode=str(raw.get("query_mode") or "aggregate").casefold(),
            fact=dict(raw.get("fact") or {}),
            joins=tuple(raw.get("joins") or []),
            metrics=tuple(raw.get("metrics") or []),
            dimensions=tuple(raw.get("dimensions") or []),
            filters=tuple(raw.get("filters") or []),
            time=dict(raw["time"]) if raw.get("time") else None,
            currency_policy=dict(raw["currency_policy"]) if raw.get("currency_policy") else None,
            resolved_slots=dict(raw.get("resolved_slots") or {}),
            ambiguity_status=str(raw.get("ambiguity_status") or "not_checked"),
            limit=int(raw.get("limit") or 100),
            time_bucket=dict(raw["time_bucket"]) if raw.get("time_bucket") else None,
            order_by=tuple(raw.get("order_by") or []),
            composition=dict(raw["composition"]) if raw.get("composition") else None,
            entity_mapping=dict(raw["entity_mapping"]) if raw.get("entity_mapping") else None,
            unique_key=str(raw["unique_key"]) if raw.get("unique_key") else None,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_id": self.source_id,
            "query_mode": self.query_mode,
            "fact": self.fact,
            "joins": [dict(j) for j in self.joins],
            "metrics": [dict(m) for m in self.metrics],
            "dimensions": [dict(d) for d in self.dimensions],
            "filters": [dict(f) for f in self.filters],
            "time": dict(self.time) if self.time else None,
            "currency_policy": dict(self.currency_policy) if self.currency_policy else None,
            "resolved_slots": self.resolved_slots,
            "ambiguity_status": self.ambiguity_status,
            "limit": self.limit,
            "time_bucket": dict(self.time_bucket) if self.time_bucket else None,
            "order_by": [dict(item) for item in self.order_by],
            "composition": dict(self.composition) if self.composition else None,
            "entity_mapping": dict(self.entity_mapping) if self.entity_mapping else None,
            "unique_key": self.unique_key,
        }


def _table_of(model_id: str) -> str:
    return model_id.rpartition(".")[2]


def _system_of(model_id: str) -> str:
    return model_id.split(".")[0] if "." in model_id else ""


def _grain_phrases(declared: str, catalog_grain: str) -> tuple[list[str], list[str]]:
    """Split grain phrases into meaningful word sequences (stop words removed).

    `one row per contract` -> [contract]; `one row per contract item` -> [contract, item].
    """
    stop = {"one", "row", "per", "the", "a", "an", "of", "for"}

    def phrases(text: str) -> list[str]:
        norm = " ".join(str(text).casefold().split())
        if not norm:
            return []
        return [w for w in norm.replace(",", " ").split() if w and w not in stop]

    return phrases(declared), phrases(catalog_grain)


def _is_field_grain(declared) -> bool:
    """A fact grain given as grain-key fields (e.g. `contract_id` or a list of
    field names) carries keys, not a descriptive noun, so descriptive containment
    is skipped; row granularity is enforced elsewhere by the grain-key list."""
    if isinstance(declared, list):
        return True
    s = str(declared or "")
    return "_id" in s or s.endswith("_id") or "." in s


def _grain_compatible(declared, catalog_grain: str) -> bool:
    """Grain compatibility via word-boundary equivalence, never substring containment.

    `contract` must NOT match `one row per contract item` (they have different
    word sequences). Shorthand `contract` is accepted only when the catalog
    phrase reduces to exactly the same [contract] sequence. A declared
    grain-key list (field-name grain) is compatible.
    """
    if declared is None or str(declared or "") == "":
        return True  # nothing claimed
    if _is_field_grain(declared):
        return True  # key-list grain; checked against the fact grain keys, not the phrase
    a, b = _grain_phrases(str(declared), str(catalog_grain))
    if not a or not b:
        return True
    return a == b


class QueryPlanValidator:
    """Recompute every claim from the catalog; emit structured issues only."""

    def __init__(self, catalog: Catalog, config: dict[str, Any] | None = None):
        self.catalog = catalog
        self.tables_by_ref = {t.ref: t for t in catalog.tables}
        qcfg = (config or {}).get("query_plan", {})
        self.schema_version = int(qcfg.get("schema_version", 1))
        self.query_modes = {str(m).casefold() for m in qcfg.get(
            "query_modes", ["aggregate", "point_lookup", "composite"])}
        self.composition_ops = {str(o).casefold() for o in qcfg.get(
            "composition_ops", ["subtract"])}
        self.aggregate_allowlist = {str(a).casefold() for a in qcfg.get(
            "aggregate_allowlist", ["sum", "count", "avg"])}
        self.filter_ops = {str(o).casefold() for o in qcfg.get(
            "filter_ops", ["eq", "ne", "gt", "gte", "lt", "lte", "in"])}
        self.role_filter_op = str(qcfg.get("role_filter_op", "eq"))
        self.timezones_allowed = {str(t) for t in qcfg.get(
            "timezones_allowed", ["Asia/Hong_Kong", "UTC"])}
        self.max_limit = int(qcfg.get("max_limit", 1000))
        self.require_currency_policy = bool(qcfg.get(
            "require_currency_policy_for_multi_currency", True))
        self.ambiguity_statuses = {str(s) for s in qcfg.get(
            "ambiguity_statuses", ["not_checked", "unresolved", "resolved", "not_applicable"])}
        self.currency_modes = {str(m) for m in qcfg.get(
            "currency_policy_modes", ["group_by_currency", "single_currency", "none"])}
        self.time_bucket_grains = {str(g).casefold() for g in qcfg.get(
            "time_bucket_grains", ["day", "month", "quarter", "year"])}
        self.ontology = catalog.ontology
        self.metrics_by_id = {str(m.get("id")): m for m in self.ontology.get("metrics", [])}
        self.relationships_by_id = {str(r.get("id")): r for r in self.ontology.get("relationships", [])}
        self.dimensions_by_id = {str(d.get("id")): d for d in self.ontology.get("dimensions", [])}
        # Role columns approved on each dimension table, parsed from the ontology
        # dimension `role_filter` (`party_role='BUYER'` -> party_role on that table).
        # A role filter in a join is only safe when it pins a single value on one of
        # these ontology-declared role columns; anything else is rejected as fan-out.
        self.role_columns_by_table: dict[str, set[str]] = {}
        for dim in self.ontology.get("dimensions", []):
            rf = str(dim.get("role_filter") or "")
            field_ref = str(dim.get("field") or "")
            dim_table = field_ref.rpartition(".")[0]
            if not dim_table:
                continue
            if "=" not in rf:
                continue
            col = rf.split("=", 1)[0].strip().strip("'\"")
            if col:
                self.role_columns_by_table.setdefault(dim_table, set()).add(col)

    # -- lookups -------------------------------------------------------------

    def _table(self, model_id: str):
        return self.tables_by_ref.get(str(model_id))

    @staticmethod
    def _is_numeric_type(data_type: str) -> bool:
        t = data_type.upper()
        return t in {"INTEGER", "REAL", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "BIGINT", "SMALLINT"} or t.startswith("INT")


    def _column(self, field_ref: str):
        """field_ref is `system.table.column`; return (Table, Column) or (None, None)."""
        table_ref, _, column_name = str(field_ref).rpartition(".")
        table = self.tables_by_ref.get(table_ref)
        if table is None:
            return None, None
        col = next((c for c in table.columns if c.name == column_name), None)
        return table, col

    # -- entry point ---------------------------------------------------------

    def validate(self, plan: QueryPlan) -> QueryPlanValidation:
        issues: list[PlanIssue] = []
        if plan.version != self.schema_version:
            issues.append(PlanIssue(ISSUE_UNKNOWN_MODEL, "version",
                                    f"unsupported schema version {plan.version}"))
        if plan.query_mode.casefold() == "composite":
            self._validate_query_mode(plan, issues)
            self._validate_composition(plan, issues)
            self._validate_limit(plan, issues)
            cross_source = any(i.issue_type == ISSUE_CROSS_SOURCE_PLAN for i in issues)
            fanout = any(i.issue_type == ISSUE_FANOUT_RISK for i in issues)
            return QueryPlanValidation(valid=not issues, issues=tuple(issues),
                                       cross_source=cross_source, fanout_risk=fanout)
        self._validate_query_mode(plan, issues)
        self._validate_fact(plan, issues)
        self._validate_source(plan, issues)
        self._validate_metrics(plan, issues)
        self._validate_joins(plan, issues)
        self._validate_dimensions(plan, issues)
        self._validate_filters(plan, issues)
        self._validate_time(plan, issues)
        self._validate_time_bucket(plan, issues)
        self._validate_order_by(plan, issues)
        self._validate_currency(plan, issues)
        self._validate_ambiguity(plan, issues)
        self._validate_unique_key(plan, issues)
        self._validate_entity_mapping(plan, issues)
        self._validate_limit(plan, issues)
        cross_source = any(i.issue_type == ISSUE_CROSS_SOURCE_PLAN for i in issues)
        fanout = any(i.issue_type == ISSUE_FANOUT_RISK for i in issues)
        return QueryPlanValidation(valid=not issues, issues=tuple(issues),
                                   cross_source=cross_source, fanout_risk=fanout)

    # -- per-section validators ---------------------------------------------

    def _validate_query_mode(self, plan: QueryPlan, issues: list[PlanIssue]):
        mode = plan.query_mode.casefold()
        if mode not in self.query_modes:
            issues.append(PlanIssue(ISSUE_UNSUPPORTED_QUERY_MODE, "query_mode",
                                    f"query_mode {plan.query_mode!r} not allowed"))
            return
        if mode == "point_lookup":
            self._validate_point_lookup(plan, issues)

    def _validate_composition(self, plan: QueryPlan, issues: list[PlanIssue]):
        spec = plan.composition or {}
        op = str(spec.get("op") or "").casefold()
        if op not in self.composition_ops:
            issues.append(PlanIssue(ISSUE_UNSUPPORTED_QUERY_MODE, "composition.op",
                                    f"composition op {spec.get('op')!r} is not allowed"))
            return
        for side in ("minuend", "subtrahend"):
            raw = spec.get(side)
            if not isinstance(raw, dict):
                issues.append(PlanIssue(ISSUE_EMPTY_METRIC_SET, f"composition.{side}",
                                        f"composition {side} must be a query plan"))
                continue
            part = QueryPlan.from_json(raw)
            if part.query_mode == "composite":
                issues.append(PlanIssue(ISSUE_UNSUPPORTED_QUERY_MODE, f"composition.{side}",
                                        "nested composite plans are not allowed"))
                continue
            nested = self.validate(part)
            for issue in nested.issues:
                issues.append(PlanIssue(issue.issue_type, f"composition.{side}.{issue.path}", issue.detail))

    def _validate_unique_key(self, plan: QueryPlan, issues: list[PlanIssue]):
        key = plan.unique_key
        if not key:
            return
        field = key if "." in key else f"{plan.fact.get('model_id')}.{key}"
        table, col = self._column(field)
        fact_ref = str(plan.fact.get("model_id") or "")
        if table is None or col is None or table.ref != fact_ref:
            issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, "unique_key",
                                    f"unique_key {key!r} is not a fact column"))

    def _validate_entity_mapping(self, plan: QueryPlan, issues: list[PlanIssue]):
        mapping = plan.entity_mapping
        if not mapping:
            return
        entity_id = str(mapping.get("entity_id") or "")
        entity = next((e for e in self.ontology.get("canonical_entities", [])
                       if str(e.get("id") or "") == entity_id), None)
        if entity is None:
            issues.append(PlanIssue(ISSUE_UNKNOWN_MODEL, "entity_mapping.entity_id",
                                    f"unknown canonical entity {entity_id!r}"))
            return
        if not entity.get("validity_required") or not entity.get("mapping_table"):
            issues.append(PlanIssue(ISSUE_RELATIONSHIP_NOT_APPROVED, "entity_mapping",
                                    "canonical mapping requires a validity window"))
        source_field = str(mapping.get("source_field_id") or "")
        table, col = self._column(source_field)
        approved = self._approved_plan_tables(plan)
        if table is None or col is None or table.ref not in approved:
            issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, "entity_mapping.source_field_id",
                                    f"mapping source {source_field!r} is not on an approved table"))
        if not mapping.get("as_of"):
            issues.append(PlanIssue(ISSUE_TIME_POLICY_REQUIRED, "entity_mapping.as_of",
                                    "canonical mapping requires an as-of date"))

    def _validate_point_lookup(self, plan: QueryPlan, issues: list[PlanIssue]):
        """A point lookup must pin an exact entity value with an equality filter on
        a primary/identifier column of the fact table (primary key, *_id column, or
        an ontology-declared identifier-pattern column such as business_identifier
        `contract_number`); otherwise it has nothing exact to return and is rejected
        rather than silently returning a table."""
        fact = self._table(plan.fact.get("model_id") or "")
        if fact is None:
            return
        exact_columns = {col.name for col in fact.columns
                         if col.primary_key or col.name.casefold().endswith("_id")}
        for item in self.ontology.get("identifier_patterns", []):
            bound_table = str(item.get("table") or "")
            if "." not in bound_table:
                bound_table = f"{item.get('system_id', '')}.{bound_table}".strip(".")
            if bound_table == fact.ref and item.get("column"):
                exact_columns.add(str(item["column"]))
        exact = False
        for f in plan.filters:
            field_ref = str(f.get("field_id") or "")
            op = str(f.get("op") or "").casefold()
            if op != "eq" or f.get("value") in (None, ""):
                continue
            table, col = self._column(field_ref)
            if col is not None and table is not None and table.ref == fact.ref \
                    and col.name in exact_columns:
                exact = True
                break
        if not exact:
            issues.append(PlanIssue(ISSUE_POINT_LOOKUP_REQUIRES_EXACT_FILTER, "filters",
                                    "point_lookup requires an exact equality filter on a "
                                    "primary-key, *_id, or ontology identifier-pattern column"))

    def _validate_fact(self, plan: QueryPlan, issues: list[PlanIssue]):
        fact = self._table(plan.fact.get("model_id") or "")
        if fact is None:
            issues.append(PlanIssue(ISSUE_UNKNOWN_MODEL, "fact.model_id",
                                    f"unknown fact model {plan.fact.get('model_id')}"))
            return
        grain = str(plan.fact.get("grain") or "")
        if grain and not _grain_compatible(grain, fact.grain):
            issues.append(PlanIssue(ISSUE_GRAIN_MISMATCH, "fact.grain",
                                    f"declared grain {grain!r} != catalog grain {fact.grain!r}"))

    def _validate_metrics(self, plan: QueryPlan, issues: list[PlanIssue]):
        if not plan.metrics:
            if plan.query_mode == "aggregate":
                issues.append(PlanIssue(ISSUE_EMPTY_METRIC_SET, "metrics",
                                        "aggregate plan defines no metrics to execute"))
            return
        fact_ref = plan.fact.get("model_id") or ""
        fact = self._table(fact_ref)
        for i, m in enumerate(plan.metrics):
            path = f"metrics[{i}]"
            agg = str(m.get("aggregate") or "").casefold()
            if agg not in self.aggregate_allowlist:
                issues.append(PlanIssue(ISSUE_UNSUPPORTED_AGGREGATE, f"{path}.aggregate",
                                        f"aggregate {m.get('aggregate')!r} not in allowlist"))
            field_ref = str(m.get("field_id") or "")
            table, col = self._column(field_ref)
            if table is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"unknown field {field_ref!r}"))
                continue
            if fact is not None and table.ref != fact.ref:
                issues.append(PlanIssue(ISSUE_METRIC_NOT_CERTIFIED, f"{path}.field_id",
                                        f"metric field {field_ref!r} is not on the fact table {fact_ref!r}"))
            if col is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"unknown column in {field_ref!r}"))
                continue
            if agg in {"sum", "avg"} and not self._is_numeric_type(col.data_type):
                issues.append(PlanIssue(ISSUE_METRIC_NOT_CERTIFIED, f"{path}.field_id",
                                        f"aggregate {agg!r} requires a numeric column, got {col.data_type!r}"))
            metric_id = str(m.get("metric_id") or "")
            metric = self.metrics_by_id.get(metric_id)
            if metric is None:
                issues.append(PlanIssue(ISSUE_METRIC_NOT_CERTIFIED, f"{path}.metric_id",
                                        f"metric {metric_id!r} not declared in the ontology"))
                continue
            if str(metric.get("model") or "") != fact_ref:
                issues.append(PlanIssue(ISSUE_METRIC_NOT_CERTIFIED, f"{path}.metric_id",
                                        f"metric {metric_id!r} belongs to {metric.get('model')}, not fact {fact_ref!r}"))
            declared_agg = str(metric.get("aggregation") or "")
            if declared_agg and declared_agg.casefold() != agg:
                issues.append(PlanIssue(ISSUE_METRIC_NOT_CERTIFIED, f"{path}.aggregate",
                                        f"aggregate {agg!r} conflicts with certified metric aggregation {declared_agg!r}"))
            declared_measure = str(metric.get("measure") or "")
            if declared_measure and declared_measure != str(col.name if col else ""):
                issues.append(PlanIssue(ISSUE_METRIC_NOT_CERTIFIED, f"{path}.field_id",
                                        f"field {field_ref!r} is not the certified measure {declared_measure!r}"))
            mgrain = str(metric.get("grain") or "")
            if mgrain and fact is not None and not _grain_compatible(mgrain, fact.grain):
                issues.append(PlanIssue(ISSUE_GRAIN_MISMATCH, f"{path}.metric_id",
                                        f"metric grain {mgrain!r} conflicts with fact grain {fact.grain!r}"))

    def _validate_joins(self, plan: QueryPlan, issues: list[PlanIssue]):
        fact_ref = plan.fact.get("model_id") or ""
        for i, join in enumerate(plan.joins):
            path = f"joins[{i}]"
            rel = self.relationships_by_id.get(str(join.get("relationship_id") or ""))
            if rel is None:
                issues.append(PlanIssue(ISSUE_RELATIONSHIP_NOT_APPROVED, f"{path}.relationship_id",
                                        f"relationship {join.get('relationship_id')!r} not declared in the ontology"))
                continue
            rel_from_table = str(rel.get("from") or "").rpartition(".")[0]
            rel_to_table = str(rel.get("to") or "").rpartition(".")[0]
            if rel_from_table != fact_ref:
                issues.append(PlanIssue(ISSUE_RELATIONSHIP_NOT_APPROVED, f"{path}.relationship_id",
                                        f"relationship starts at {rel_from_table!r}, not the fact {fact_ref!r}"))
            dimension_ref = str(join.get("dimension_model_id") or "")
            if dimension_ref and dimension_ref != rel_to_table:
                issues.append(PlanIssue(ISSUE_RELATIONSHIP_NOT_APPROVED, f"{path}.dimension_model_id",
                                        f"join target {dimension_ref!r} != relationship target {rel_to_table!r}"))
            if self._table(rel_to_table) is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_MODEL, f"{path}.dimension_model_id",
                                        f"unknown dimension model {rel_to_table!r}"))
            if self._table(fact_ref) is None:
                continue
            if _system_of(rel_to_table) and _system_of(rel_to_table) != _system_of(fact_ref):
                issues.append(PlanIssue(ISSUE_CROSS_SOURCE_PLAN, f"{path}.relationship_id",
                                        f"relationship crosses systems: fact {fact_ref!r} vs dimension {rel_to_table!r}"))
            self._check_cardinality(plan, join, rel, rel_to_table, path, issues)

    def _check_cardinality(self, plan: QueryPlan, join: dict[str, Any], rel: dict[str, Any],
                           dimension_ref: str, path: str, issues: list[PlanIssue]):
        """Fan-out protection: never emit a join that can multiply a measure."""
        cardinality = str(rel.get("cardinality") or "").casefold()
        fanout_warning = str(rel.get("fanout_warning") or "")
        if fanout_warning:
            issues.append(PlanIssue(ISSUE_FANOUT_RISK, path,
                                    f"relationship carries fanout warning: {fanout_warning}"))
            return
        if cardinality in {"one_to_one", "many_to_one"}:
            return
        if cardinality == "one_to_many":
            if self._current_filter_restores_grain(join, rel, dimension_ref, path, issues):
                return
            role_filter = join.get("role_filter")
            required = bool(rel.get("role_filter_required"))
            if role_filter and required:
                field_ref = str(role_filter.get("field_id") or "")
                op = str(role_filter.get("op") or "").casefold()
                value = str(role_filter.get("value") or "")
                if op != self.role_filter_op:
                    issues.append(PlanIssue(ISSUE_FANOUT_RISK, f"{path}.role_filter.op",
                                            f"role filter op must be {self.role_filter_op!r}"))
                if not value:
                    issues.append(PlanIssue(ISSUE_FANOUT_RISK, f"{path}.role_filter.value",
                                            "role filter requires a non-empty value"))
                if "," in value:
                    issues.append(PlanIssue(ISSUE_FANOUT_RISK, f"{path}.role_filter.value",
                                            "role filter must pin exactly one role value (no multi-value list)"))
                dim_table, col = self._column(field_ref)
                if dim_table is None or dim_table.ref != dimension_ref:
                    issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.role_filter.field_id",
                                            f"role filter field {field_ref!r} is not on dimension {dimension_ref!r}"))
                elif col is None:
                    issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.role_filter.field_id",
                                            f"unknown column in {field_ref!r}"))
                else:
                    approved_role_cols = self.role_columns_by_table.get(dimension_ref, set())
                    if col.name not in approved_role_cols:
                        issues.append(PlanIssue(ISSUE_FANOUT_RISK, f"{path}.role_filter.field_id",
                                                f"role filter {col.name!r} is not an ontology-declared role column "
                                                f"on {dimension_ref!r} (approved: {sorted(approved_role_cols) or 'none'})"))
                return
            issues.append(PlanIssue(ISSUE_FANOUT_RISK, path,
                                    f"1:N relationship {rel.get('id')} is only safe with an "
                                    "approved role filter that restores one row per fact key"))
            return
        if cardinality == "one_to_many_over_time":
            issues.append(PlanIssue(ISSUE_FANOUT_RISK, path,
                                    f"snapshot relationship {rel.get('id')} needs an as-of-time plan; "
                                    "not supported in this slice"))
            return
        issues.append(PlanIssue(ISSUE_RELATIONSHIP_NOT_APPROVED, path,
                                f"relationship cardinality {cardinality!r} is not approved"))

    def _current_filter_restores_grain(
        self,
        join: dict[str, Any],
        rel: dict[str, Any],
        dimension_ref: str,
        path: str,
        issues: list[PlanIssue],
    ) -> bool:
        declared = parse_declared_filter(str(rel.get("current_filter") or ""))
        pinned = join.get("current_filter")
        if declared is None or not isinstance(pinned, dict):
            return False
        field_ref = str(pinned.get("field_id") or "")
        op = str(pinned.get("op") or "").casefold()
        value = pinned.get("value")
        expected_field = f"{dimension_ref}.{declared['column']}"
        if field_ref != expected_field or op != declared["op"] or str(value) != str(declared["value"]):
            issues.append(PlanIssue(ISSUE_FANOUT_RISK, f"{path}.current_filter",
                                    "current_filter must match the ontology declaration"))
            return True
        table, col = self._column(field_ref)
        if table is None or table.ref != dimension_ref or col is None:
            issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.current_filter.field_id",
                                    f"current_filter field {field_ref!r} is not on {dimension_ref!r}"))
        return True

    def _validate_dimensions(self, plan: QueryPlan, issues: list[PlanIssue]):
        approved_tables = self._approved_plan_tables(plan)
        for i, dim in enumerate(plan.dimensions):
            path = f"dimensions[{i}]"
            field_ref = str(dim.get("field_id") or "")
            table, col = self._column(field_ref)
            if table is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"unknown field {field_ref!r}"))
                continue
            if table.ref not in approved_tables:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"dimension field {field_ref!r} is not reachable through an approved join"))
            if col is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"unknown column in {field_ref!r}"))
            dim_id = str(dim.get("dimension_id") or "")
            if dim_id and dim_id not in self.dimensions_by_id:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.dimension_id",
                                        f"dimension {dim_id!r} not declared in the ontology"))

    def _approved_plan_tables(self, plan: QueryPlan) -> set[str]:
        """Tables a plan may reference: the fact plus approved join targets."""
        approved = {plan.fact.get("model_id") or ""}
        for join in plan.joins:
            rel = self.relationships_by_id.get(str(join.get("relationship_id") or ""))
            if rel is None:
                continue
            rel_to_table = str(rel.get("to") or "").rpartition(".")[0]
            if str(rel.get("cardinality") or "") in {"one_to_one", "many_to_one"}:
                approved.add(rel_to_table)
                continue
            if join.get("role_filter") and bool(rel.get("role_filter_required")):
                approved.add(rel_to_table)
                continue
            if join.get("current_filter") and rel.get("current_filter"):
                approved.add(rel_to_table)
        return approved

    def _validate_filters(self, plan: QueryPlan, issues: list[PlanIssue]):
        approved_tables = self._approved_plan_tables(plan)
        for i, f in enumerate(plan.filters):
            path = f"filters[{i}]"
            field_ref = str(f.get("field_id") or "")
            table, col = self._column(field_ref)
            if table is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"unknown field {field_ref!r}"))
                continue
            if table.ref not in approved_tables:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"filter field {field_ref!r} is not reachable through approved tables"))
            if col is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.field_id",
                                        f"unknown column in {field_ref!r}"))
            op = str(f.get("op") or "").casefold()
            if op not in self.filter_ops:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.op",
                                        f"filter op {f.get('op')!r} not supported"))
            if op == "in":
                value = f.get("value")
                if not isinstance(value, list) or not value or any(item is None or item == "" for item in value):
                    issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.value",
                                            "IN filter requires a non-empty list"))
            elif f.get("value") is None and op not in {"ne", "gt", "gte", "lt", "lte"}:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"{path}.value",
                                        "filter requires a value"))

    def _validate_time(self, plan: QueryPlan, issues: list[PlanIssue]):
        t = plan.time
        if t is None:
            return
        field_ref = str(t.get("field_id") or "")
        if self._column(field_ref)[0] is None:
            issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, "time.field_id",
                                    f"unknown field {field_ref!r}"))
        has_start = t.get("start") is not None
        has_end = t.get("end_exclusive") is not None
        tz = str(t.get("timezone") or "")
        if (has_start or has_end) and not tz:
            issues.append(PlanIssue(ISSUE_TIME_POLICY_REQUIRED, "time.timezone",
                                    "half-open time range requires an explicit timezone when the boundary can shift"))
        elif tz and tz not in self.timezones_allowed:
            issues.append(PlanIssue(ISSUE_TIME_POLICY_REQUIRED, "time.timezone",
                                    f"timezone {tz!r} not allowed"))

    def _validate_time_bucket(self, plan: QueryPlan, issues: list[PlanIssue]):
        bucket = plan.time_bucket
        if bucket is None:
            return
        grain = str(bucket.get("grain") or "").casefold()
        if grain not in self.time_bucket_grains:
            issues.append(PlanIssue(ISSUE_TIME_POLICY_REQUIRED, "time_bucket.grain",
                                    f"grain {bucket.get('grain')!r} not allowed"))
        field_ref = str(bucket.get("field_id") or "")
        table, col = self._column(field_ref)
        if table is None or col is None:
            issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, "time_bucket.field_id",
                                    f"unknown field {field_ref!r}"))
            return
        fact_ref = str(plan.fact.get("model_id") or "")
        if table.ref != fact_ref:
            issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, "time_bucket.field_id",
                                    f"time bucket field {field_ref!r} is not on the fact table"))
            return
        approved = {
            str(dim.get("field") or "")
            for dim in self.ontology.get("dimensions", [])
            if dim.get("time_role") and str(dim.get("field") or "").rpartition(".")[0] == fact_ref
        }
        if field_ref not in approved:
            issues.append(PlanIssue(ISSUE_TIME_POLICY_REQUIRED, "time_bucket.field_id",
                                    f"field {field_ref!r} is not an ontology time_role on {fact_ref!r}"))

    def _validate_order_by(self, plan: QueryPlan, issues: list[PlanIssue]):
        if not plan.order_by:
            return
        if plan.query_mode != "aggregate":
            issues.append(PlanIssue(ISSUE_UNSUPPORTED_QUERY_MODE, "order_by",
                                    "order_by is only allowed on aggregate plans"))
            return
        if not plan.metrics:
            issues.append(PlanIssue(ISSUE_EMPTY_METRIC_SET, "order_by",
                                    "order_by requires a metric"))
            return
        for i, item in enumerate(plan.order_by):
            target = str(item.get("target") or "")
            if target != "metric_0":
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, f"order_by[{i}].target",
                                        f"order_by target {target!r} is not allowed"))

    def _validate_currency(self, plan: QueryPlan, issues: list[PlanIssue]):
        fact = self._table(plan.fact.get("model_id") or "")
        if fact is None:
            return
        has_currency_column = any(c.name.casefold() == "currency" for c in fact.columns)
        if not has_currency_column:
            return
        policy = plan.currency_policy
        if policy is None:
            mode = ""
        else:
            mode = str(policy.get("mode") or "").casefold()
        if mode not in self.currency_modes:
            issues.append(PlanIssue(ISSUE_CURRENCY_POLICY_REQUIRED, "currency_policy.mode",
                                    f"mode {policy and policy.get('mode')!r} not allowed"))
            return
        if mode == "group_by_currency":
            currency_field = str(policy.get("currency_field_id") or "")
            if not currency_field:
                issues.append(PlanIssue(ISSUE_CURRENCY_POLICY_REQUIRED, "currency_policy.currency_field_id",
                                        "group_by_currency requires currency_field_id"))
            elif self._column(currency_field)[0] is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, "currency_policy.currency_field_id",
                                        f"unknown field {currency_field!r}"))
            return
        if mode == "single_currency":
            currency_field = str(policy.get("currency_field_id") or "")
            value = str(policy.get("currency_value") or "").strip()
            if not currency_field:
                issues.append(PlanIssue(ISSUE_CURRENCY_POLICY_REQUIRED, "currency_policy.currency_field_id",
                                        "single_currency requires currency_field_id"))
            elif self._column(currency_field)[0] is None:
                issues.append(PlanIssue(ISSUE_UNKNOWN_FIELD, "currency_policy.currency_field_id",
                                        f"unknown field {currency_field!r}"))
            if not value:
                issues.append(PlanIssue(ISSUE_CURRENCY_POLICY_REQUIRED, "currency_policy.currency_value",
                                        "single_currency requires a currency value"))
            return
        # mode == "none": a multi-currency fact still needs an explicit policy.
        if self.require_currency_policy:
            issues.append(PlanIssue(ISSUE_CURRENCY_POLICY_REQUIRED, "currency_policy",
                                    "multi-currency fact requires group_by_currency or single_currency"))

    def _validate_ambiguity(self, plan: QueryPlan, issues: list[PlanIssue]):
        status = plan.ambiguity_status.casefold()
        if status not in self.ambiguity_statuses:
            issues.append(PlanIssue(ISSUE_UNRESOLVED_AMBIGUITY, "ambiguity_status",
                                    f"ambiguity status {plan.ambiguity_status!r} not allowed"))
            return
        if status == "unresolved":
            issues.append(PlanIssue(ISSUE_UNRESOLVED_AMBIGUITY, "ambiguity_status",
                                    "unresolved ambiguity blocks execution"))
            return
        if status == "not_checked" and plan.metrics:
            unmet = []
            for m in plan.metrics:
                metric_id = str(m.get("metric_id") or "")
                slot = plan.resolved_slots.get("metric")
                if not slot or (slot.get("value") is not None and str(slot.get("value")) != metric_id):
                    unmet.append(metric_id)
            for d in plan.dimensions:
                dim_id = str(d.get("dimension_id") or "")
                if dim_id and dim_id not in plan.resolved_slots:
                    unmet.append(dim_id)
            if unmet:
                issues.append(PlanIssue(ISSUE_UNRESOLVED_AMBIGUITY, "resolved_slots",
                                        f"slots missing explicit resolution: {', '.join(unmet)}"))

    def _validate_source(self, plan: QueryPlan, issues: list[PlanIssue]):
        """The declared source must be the system of every referenced table."""
        source_id = str(plan.source_id or "")
        if not source_id:
            issues.append(PlanIssue(ISSUE_UNKNOWN_MODEL, "source_id", "plan has no source_id"))
            return
        referenced = {plan.fact.get("model_id") or ""}
        for m in plan.metrics:
            referenced.add(str(m.get("field_id") or ""))
        for d in plan.dimensions:
            referenced.add(str(d.get("field_id") or ""))
        for f in plan.filters:
            referenced.add(str(f.get("field_id") or ""))
        for join in plan.joins:
            rel = self.relationships_by_id.get(str(join.get("relationship_id") or ""))
            if rel is not None:
                referenced.add(str(rel.get("to") or ""))
        for ref in referenced:
            table_id = str(ref).rpartition(".")[0]
            if not table_id:
                continue
            sys_of_ref = _system_of(table_id)
            if sys_of_ref and sys_of_ref != source_id:
                issues.append(PlanIssue(ISSUE_CROSS_SOURCE_PLAN, "source_id",
                                        f"source {source_id!r} does not contain reference {table_id!r}"))

    def _validate_limit(self, plan: QueryPlan, issues: list[PlanIssue]):
        limit = int(plan.limit or 0)
        if limit < 1:
            issues.append(PlanIssue(ISSUE_QUERY_BUDGET_EXCEEDED, "limit",
                                    f"limit must be a positive integer, got {plan.limit!r}"))
        elif limit > self.max_limit:
            issues.append(PlanIssue(ISSUE_QUERY_BUDGET_EXCEEDED, "limit",
                                    f"limit {limit} exceeds max_limit {self.max_limit}"))
