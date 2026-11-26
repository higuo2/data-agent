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


def _subtract_metric_rows(left: dict[str, Any], right: dict[str, Any]) -> list[tuple]:
    left_cols = list(left.get("columns") or [])
    right_cols = list(right.get("columns") or [])
    if "metric_0" not in left_cols:
        return [tuple(row) for row in left.get("rows") or []]
    left_m = left_cols.index("metric_0")
    right_m = right_cols.index("metric_0") if "metric_0" in right_cols else None

    def key_of(cols: list[str], row: tuple, metric_idx: int) -> tuple:
        return tuple(row[i] for i in range(len(cols)) if i != metric_idx)

    right_map = {}
    if right_m is not None:
        for row in right.get("rows") or []:
            right_map[key_of(right_cols, row, right_m)] = row[right_m] or 0
    out = []
    seen = set()
    for row in left.get("rows") or []:
        row = list(row)
        key = key_of(left_cols, tuple(row), left_m)
        seen.add(key)
        row[left_m] = (row[left_m] or 0) - right_map.get(key, 0)
        out.append(tuple(row))
    for key, value in right_map.items():
        if key in seen:
            continue
        new_row = []
        right_row = next(
            (r for r in (right.get("rows") or []) if key_of(right_cols, r, right_m or 0) == key),
            None,
        )
        for col in left_cols:
            if col == "metric_0":
                new_row.append(-(value or 0))
            elif right_row is not None and col in right_cols:
                new_row.append(right_row[right_cols.index(col)])
            else:
                new_row.append(None)
        out.append(tuple(new_row))
    return out


class SqlRenderer:
    """Render a validated QueryPlan into SQL text plus bound parameters."""

    def __init__(self, time_bucket_sql: dict[str, str] | None = None):
        self.time_bucket_sql = time_bucket_sql or {
            "day": "strftime('%Y-%m-%d', {col})",
            "month": "strftime('%Y-%m', {col})",
            "year": "strftime('%Y', {col})",
            "quarter": "strftime('%Y', {col}) || 'Q' || ((CAST(strftime('%m', {col}) AS INTEGER) - 1) / 3 + 1)",
        }

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
        if plan.time_bucket:
            expr = self._bucket_expr(plan, catalog)
            selects.append(f"{expr} AS {_quote('time_bucket')}")
            group_cols.append(expr)

        from_sql = self._from_where(plan, catalog, params)
        sql = f"SELECT {', '.join(selects)} {from_sql}"
        if group_cols:
            sql += " GROUP BY " + ", ".join(group_cols)
        if plan.order_by:
            orders = []
            for item in plan.order_by:
                target = str(item.get("target") or "")
                if not _IDENT_RE.fullmatch(target):
                    raise ValueError(f"identifier not allowed: {target!r}")
                direction = "DESC" if item.get("descending") else "ASC"
                orders.append(f"{_quote(target)} {direction}")
            sql += " ORDER BY " + ", ".join(orders)
        sql += f" LIMIT {int(plan.limit)}"
        return sql, params

    def _bucket_expr(self, plan: QueryPlan, catalog: Catalog) -> str:
        grain = str((plan.time_bucket or {}).get("grain") or "").casefold()
        template = self.time_bucket_sql.get(grain)
        if not template or "{col}" not in template:
            raise ValueError(f"unsupported time bucket grain {grain!r}")
        alias, _, column, _ = self._qualified(str((plan.time_bucket or {}).get("field_id") or ""), plan, catalog)
        return template.replace("{col}", f"{alias}.{_quote(column)}")

    def _from_where(self, plan: QueryPlan, catalog: Catalog, params: list[Any]) -> str:
        fact_table = str(plan.fact.get("model_id") or "").rsplit(".", 1)[-1]
        sql = f"FROM {_quote(fact_table)} AS f"
        for i, join in enumerate(plan.joins):
            rel = self._rel(join.get("relationship_id"), catalog)
            _, _, f_col = _split_field(str(rel.get("from") or ""))
            _, d_tbl, d_col = _split_field(str(rel.get("to") or ""))
            on = f"f.{_quote(f_col)} = d{i}.{_quote(d_col)}"
            for extra in (join.get("role_filter"), join.get("current_filter")):
                if not extra:
                    continue
                _, _, extra_col = _split_field(str(extra.get("field_id") or ""))
                params.append(extra.get("value"))
                on += f" AND d{i}.{_quote(extra_col)} = ?"
            sql += f" INNER JOIN {_quote(d_tbl)} AS d{i} ON {on}"
        where = self._build_where(plan.filters, plan, params)
        where.extend(self._time_clauses(plan, params))
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql

    def uniqueness_sql(self, plan: QueryPlan, catalog: Catalog, unique_key: str) -> tuple[str, list[Any]]:
        self._catalog = catalog
        params: list[Any] = []
        field = unique_key if "." in unique_key else f"{plan.fact.get('model_id')}.{unique_key}"
        alias, _, column, _ = self._qualified(field, plan, catalog)
        from_sql = self._from_where(plan, catalog, params)
        sql = (
            f"SELECT 1 {from_sql} GROUP BY {alias}.{_quote(column)} "
            f"HAVING COUNT(*) > 1 LIMIT 1"
        )
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
        qcfg = (config or {}).get("query_plan", {})
        self.renderer = SqlRenderer(qcfg.get("time_bucket_sql"))

    def execute(self, plan: QueryPlan) -> dict[str, Any]:
        validation = self.validator.validate(plan)
        if not validation.valid:
            return {
                "ok": False,
                "issues": [i.issue_type for i in validation.issues],
                "details": [f"{i.path}: {i.detail}" for i in validation.issues],
            }
        if str(plan.query_mode or "").casefold() == "composite":
            return self._execute_composite(plan)
        if plan.unique_key:
            probe = self._probe_unique_key(plan)
            if probe is not None:
                return probe
        sql, params = self.renderer.render(plan, self.catalog)
        table = self._fact_table(plan)
        cols, rows = self._query(table.database_file, sql, params)
        if plan.entity_mapping:
            return self._apply_mapping(plan, sql, params, cols, rows)
        return {"ok": True, "sql": sql, "params": params, "columns": cols, "rows": rows, "row_count": len(rows)}

    def _query(self, database_file, sql: str, params: list[Any]) -> tuple[list[str], list[tuple]]:
        con = sqlite3.connect(f"file:{database_file}?mode=ro", uri=True, timeout=3)
        try:
            con.execute("PRAGMA query_only=ON")
            cursor = con.execute(sql, params)
            cols = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
        finally:
            con.close()
        return cols, rows

    def _probe_unique_key(self, plan: QueryPlan) -> dict[str, Any] | None:
        sql, params = self.renderer.uniqueness_sql(plan, self.catalog, str(plan.unique_key))
        table = self._fact_table(plan)
        _, rows = self._query(table.database_file, sql, params)
        if rows:
            return {
                "ok": False,
                "issues": ["fanout_risk"],
                "details": [f"unique_key {plan.unique_key!r} is not unique after filters"],
            }
        return None

    def _execute_composite(self, plan: QueryPlan) -> dict[str, Any]:
        spec = plan.composition or {}
        left = self.execute(QueryPlan.from_json(spec.get("minuend") or {}))
        if not left.get("ok"):
            return left
        right = self.execute(QueryPlan.from_json(spec.get("subtrahend") or {}))
        if not right.get("ok"):
            return right
        rows = _subtract_metric_rows(left, right)
        return {
            "ok": True,
            "sql": f"{left.get('sql')}; {right.get('sql')}",
            "params": list(left.get("params") or []) + list(right.get("params") or []),
            "columns": list(left.get("columns") or []),
            "rows": rows,
            "row_count": len(rows),
        }

    def _apply_mapping(
        self,
        plan: QueryPlan,
        sql: str,
        params: list[Any],
        columns: list[str],
        rows: list[tuple],
    ) -> dict[str, Any]:
        mapping = plan.entity_mapping or {}
        entity = next(
            (item for item in self.catalog.ontology.get("canonical_entities", [])
             if str(item.get("id") or "") == str(mapping.get("entity_id") or "")),
            None,
        )
        if entity is None:
            return {"ok": False, "issues": ["unknown_model"], "details": ["canonical entity missing"]}
        map_ref = str(entity.get("mapping_table") or "")
        map_table = next((t for t in self.catalog.tables if t.ref == map_ref), None)
        if map_table is None:
            return {"ok": False, "issues": ["unknown_model"], "details": [f"unknown mapping table {map_ref}"]}
        source_field = str(mapping.get("source_field_id") or "")
        dim_index = next(
            (i for i, d in enumerate(plan.dimensions) if d.get("field_id") == source_field),
            None,
        )
        if dim_index is None:
            return {"ok": False, "issues": ["unknown_field"], "details": ["mapping dimension missing"]}
        dim_col = f"dim_{dim_index}"
        if dim_col not in columns:
            return {"ok": False, "issues": ["unknown_field"], "details": [f"missing {dim_col}"]}
        as_of = str(mapping.get("as_of") or "")
        source_system = source_field.split(".", 1)[0]
        cols = dict(entity.get("mapping_columns") or {})
        source_col = str(cols.get("source_key") or "")
        canonical_col = str(cols.get("canonical_key") or "")
        system_col = str(cols.get("source_system") or "")
        from_col = str(cols.get("valid_from") or "")
        to_col = str(cols.get("valid_to") or "")
        required = [source_col, canonical_col, system_col, from_col, to_col, map_table.name]
        if any(not _IDENT_RE.fullmatch(name) for name in required):
            return {"ok": False, "issues": ["unknown_field"], "details": ["mapping columns are not identifiers"]}
        map_sql = (
            f"SELECT {_quote(source_col)}, {_quote(canonical_col)} FROM {_quote(map_table.name)} "
            f"WHERE {_quote(system_col)} = ? AND {_quote(from_col)} <= ? "
            f"AND ({_quote(to_col)} IS NULL OR {_quote(to_col)} >= ?)"
        )
        _, map_rows = self._query(map_table.database_file, map_sql, [source_system, as_of, as_of])
        mapped: dict[str, str] = {}
        for source_id, canonical_id in map_rows:
            if source_id in mapped and mapped[source_id] != canonical_id:
                return {
                    "ok": False,
                    "issues": ["fanout_risk"],
                    "details": [f"source id {source_id!r} maps to multiple canonical keys"],
                }
            mapped[str(source_id)] = str(canonical_id)
        metric_idx = columns.index("metric_0") if "metric_0" in columns else 0
        dim_idx = columns.index(dim_col)
        grouped: dict[tuple, list] = {}
        for row in rows:
            canonical = mapped.get(str(row[dim_idx]))
            if not canonical:
                continue
            new_row = list(row)
            new_row[dim_idx] = canonical
            key = tuple(new_row[i] if i != metric_idx else None for i in range(len(new_row)))
            current = grouped.get(key)
            if current is None:
                grouped[key] = new_row
            else:
                current[metric_idx] = (current[metric_idx] or 0) + (new_row[metric_idx] or 0)
        aligned = list(grouped.values())
        aligned.sort(key=lambda item: item[metric_idx] or 0, reverse=True)
        top_n = mapping.get("top_n")
        if top_n:
            aligned = aligned[: int(top_n)]
        if entity.get("label_field") and aligned:
            aligned = self._relabel(entity, dim_idx, aligned)
        out_rows = [tuple(item) for item in aligned]
        return {
            "ok": True,
            "sql": sql,
            "params": params,
            "columns": columns,
            "rows": out_rows,
            "row_count": len(out_rows),
        }

    def _relabel(self, entity: dict[str, Any], dim_idx: int, rows: list[list]) -> list[list]:
        label_field = str(entity.get("label_field") or "")
        table_ref, _, column = label_field.rpartition(".")
        table = next((t for t in self.catalog.tables if t.ref == table_ref), None)
        if table is None or not _IDENT_RE.fullmatch(column):
            return rows
        ids = [row[dim_idx] for row in rows]
        marks = ", ".join("?" for _ in ids)
        id_field = str(entity.get("canonical_key") or "").rpartition(".")[2]
        if not _IDENT_RE.fullmatch(id_field):
            return rows
        label_sql = (
            f"SELECT {_quote(id_field)}, {_quote(column)} FROM {_quote(table.name)} "
            f"WHERE {_quote(id_field)} IN ({marks})"
        )
        _, labels = self._query(table.database_file, label_sql, ids)
        names = {str(ident): label for ident, label in labels}
        for row in rows:
            row[dim_idx] = names.get(str(row[dim_idx]), row[dim_idx])
        return rows

    def _fact_table(self, plan: QueryPlan) -> Table:
        model_id = str(plan.fact.get("model_id") or "")
        return next(t for t in self.catalog.tables if t.ref == model_id)
