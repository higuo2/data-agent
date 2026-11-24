"""Phase 2 SQL rendering and read-only execution over the synthetic SQLite sources.

The renderer is bounded to the contract: only SELECT over one fact source,
catalog-approved dimension joins on role-filtered dims, typed WHERE/GROUP BY,
aggregates SUM/COUNT/AVG, bound values, and an allowlist for SQL identifiers.
Identifiers are resolved exclusively from catalog model/field ids; every
literal is parameterized. It never renders mutation SQL, never introduces
subqueries/CTEs/window functions, and refuses plan shapes the validator
rejects.
"""
from __future__ import annotations
import re
import sqlite3
from typing import Any
from .catalog import Catalog, Table
from .queryplan import QueryPlan, QueryPlanValidator

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_OP_SQL = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


def _quote(ident: str) -> str:
    if not _IDENT_RE.fullmatch(ident):
        raise ValueError(f"identifier not allowed: {ident!r}")
    return f'"{ident}"'


def _split_field(field_ref: str) -> tuple[str, str, str]:
    """`system.table.column` -> (system, table, column)."""
    parts = str(field_ref).split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"field reference must be system.table.column: {field_ref!r}")
    return parts[0], parts[1], parts[2]


def _agg_alias(idx: int) -> str:
    return f"metric_{idx}"


def _dim_alias(idx: int) -> str:
    return f"dim_{idx}"


class SqlRenderer:
    """Render a validated QueryPlan into SQL text plus bound parameters."""

    def render(self, plan: QueryPlan, catalog: Catalog) -> tuple[str, list[Any]]:
        self._catalog = catalog
        mode = str(plan.query_mode or "").casefold()
        if mode == "point_lookup":
            return self._render_point_lookup(plan)
        return self._render_aggregate(plan, catalog)

    # -- helpers -----------------------------------------------------------

    def _rel(self, relationship_id: Any, catalog: Catalog) -> dict[str, Any]:
        if catalog is None:
            catalog = getattr(self, "_catalog", None)
        rel_map = {str(r.get("id")): r for r in catalog.ontology.get("relationships", [])}
        rel = rel_map.get(str(relationship_id or ""))
        if rel is None:
            raise ValueError(f"unapproved or unknown relationship {relationship_id!r}")
        return rel

    def _rel_dim_table(self, join: dict[str, Any], rel: dict[str, Any]) -> str:
        dim_ref = str(join.get("dimension_model_id") or "")
        if dim_ref:
            return dim_ref
        return str(rel.get("to") or "").rpartition(".")[0]

    def _qualified(self, field_ref: str, plan: QueryPlan, catalog: Catalog) -> tuple[str, str, str, str]:
        """Return (alias, table, column, fully-qualified SQL ref) for a field."""
        _, table, column = _split_field(field_ref)
        alias = self._table_alias(field_ref.rsplit(".", 1)[0], plan, catalog)
        return alias, table, column, f"{alias}.{_quote(column)}"

    # -- aggregate ---------------------------------------------------------

    def _render_aggregate(self, plan: QueryPlan, catalog: Catalog) -> tuple[str, list[Any]]:
        self._catalog = catalog
        params: list[Any] = []
        fact_table = str(plan.fact.get("model_id") or "").rsplit(".", 1)[-1]

        selects: list[str] = []
        group_cols: list[str] = []
        for i, m in enumerate(plan.metrics):
            alias, _, column, _ = self._qualified(m["field_id"], plan, catalog)
            agg = str(m.get("aggregate") or "").casefold()
            if agg == "count":
                selects.append(f"COUNT(*) AS {_quote(_agg_alias(i))}")
            else:
                selects.append(f"{agg.upper()}({alias}.{_quote(column)}) AS {_quote(_agg_alias(i))}")
        for i, d in enumerate(plan.dimensions):
            alias, _, column, _ = self._qualified(d["field_id"], plan, catalog)
            selects.append(f"{alias}.{_quote(column)} AS {_quote(_dim_alias(i))}")
            group_cols.append(f"{alias}.{_quote(column)}")
        if plan.currency_policy and str(plan.currency_policy.get("mode") or "").casefold() == "group_by_currency":
            alias, _, column, _ = self._qualified(plan.currency_policy["currency_field_id"], plan, catalog)
            selects.append(f"{alias}.{_quote(column)} AS {_quote('currency')}")
            group_cols.append(f"{alias}.{_quote(column)}")

        sql = f"SELECT {', '.join(selects)} FROM {_quote(fact_table)} AS f"
        for i, join in enumerate(plan.joins):
            rel = self._rel(join.get("relationship_id"), catalog)
            _, _, f_col = _split_field(str(rel.get("from") or ""))
            _, d_tbl, d_col = _split_field(str(rel.get("to") or ""))
            on = f"f.{_quote(f_col)} = d{i}.{_quote(d_col)}"
            role = join.get("role_filter")
            if role:
                _, _, role_col = _split_field(str(role.get("field_id") or ""))
                value = str(role.get("value") or "")
                params.append(value)
                on += f" AND d{i}.{_quote(role_col)} = ?"
            sql += f" INNER JOIN {_quote(d_tbl)} AS d{i} ON {on}"

        where = self._build_where(plan.filters, plan, params)
        where.extend(self._time_clauses(plan, params))
        if where:
            sql += " WHERE " + " AND ".join(where)
        if group_cols:
            sql += " GROUP BY " + ", ".join(group_cols)
        sql += f" LIMIT {int(plan.limit)}"
        return sql, params

    # -- point lookup ------------------------------------------------------

    def _render_point_lookup(self, plan: QueryPlan) -> tuple[str, list[Any]]:
        params: list[Any] = []
        fact_table = str(plan.fact.get("model_id") or "").rsplit(".", 1)[-1]
        where = self._build_where(plan.filters, plan, params)
        where.extend(self._time_clauses(plan, params))
        sql = f"SELECT * FROM {_quote(fact_table)} AS f"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" LIMIT {int(plan.limit)}"
        return sql, params

    # -- filters -----------------------------------------------------------

    def _build_where(self, filters: list[dict[str, Any]], plan: QueryPlan, params: list[Any]) -> list[str]:
        clauses: list[str] = []
        for f in filters:
            field_ref = str(f.get("field_id") or "")
            alias, _, column, _ = self._qualified(field_ref, plan, getattr(self, "_catalog", None))
            op = str(f.get("op") or "").casefold()
            if op == "in":
                value = f.get("value")
                if not isinstance(value, list) or not value:
                    raise ValueError("IN filter requires a non-empty list value")
                params.extend(value)
                marks = ", ".join("?" for _ in value)
                clauses.append(f"{alias}.{_quote(column)} IN ({marks})")
            elif op in _OP_SQL:
                params.append(f.get("value"))
                clauses.append(f"{alias}.{_quote(column)} {_OP_SQL[op]} ?")
            else:
                raise ValueError(f"unsupported filter op {op!r}")
        return clauses

    def _time_clauses(self, plan: QueryPlan, params: list[Any]) -> list[str]:
        """Half-open [start, end_exclusive) bounds. The validator already required a timezone."""
        window = plan.time or {}
        field_ref = str(window.get("field_id") or "")
        if not field_ref or (window.get("start") is None and window.get("end_exclusive") is None):
            return []
        alias, _, column, _ = self._qualified(field_ref, plan, self._catalog)
        clauses: list[str] = []
        if window.get("start") is not None:
            params.append(window.get("start"))
            clauses.append(f"{alias}.{_quote(column)} >= ?")
        if window.get("end_exclusive") is not None:
            params.append(window.get("end_exclusive"))
            clauses.append(f"{alias}.{_quote(column)} < ?")
        return clauses

    # -- alias resolution --------------------------------------------------

    def _table_alias(self, table_ref: str, plan: QueryPlan, catalog: Catalog | None = None) -> str:
        model_id = str(plan.fact.get("model_id") or "")
        if table_ref == model_id or table_ref == model_id.rsplit(".", 1)[-1]:
            return "f"
        if catalog is None:
            catalog = getattr(self, "_catalog", None)
        for i, join in enumerate(plan.joins):
            rel = self._rel(join.get("relationship_id"), catalog)
            dim_ref = self._rel_dim_table(join, rel)
            if table_ref == dim_ref or table_ref == dim_ref.rsplit(".", 1)[-1]:
                return f"d{i}"
        raise ValueError(f"field table {table_ref!r} not reachable in plan")


class QueryExecutor:
    """Execute a validated plan against its read-only SQLite source; never mutates."""

    def __init__(self, catalog: Catalog, config: dict[str, Any] | None = None):
        self.catalog = catalog
        self.validator = QueryPlanValidator(catalog, config)
        self.renderer = SqlRenderer()

    def execute(self, plan: QueryPlan) -> dict[str, Any]:
        validation = self.validator.validate(plan)
        if not validation.valid:
            return {
                "ok": False,
                "issues": [i.issue_type for i in validation.issues],
                "details": [f"{i.path}: {i.detail}" for i in validation.issues],
            }
        sql, params = self.renderer.render(plan, self.catalog)
        table = self._fact_table(plan)
        con = sqlite3.connect(f"file:{table.database_file}?mode=ro", uri=True, timeout=3)
        try:
            con.execute("PRAGMA query_only=ON")
            cursor = con.execute(sql, params)
            cols = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
        finally:
            con.close()
        return {"ok": True, "sql": sql, "params": params, "columns": cols, "rows": rows, "row_count": len(rows)}

    def _fact_table(self, plan: QueryPlan) -> Table:
        model_id = str(plan.fact.get("model_id") or "")
        return next(t for t in self.catalog.tables if t.ref == model_id)
