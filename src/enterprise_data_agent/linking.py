# 实体链接：从问题里抽出编号，只在声明的表和列上做只读、参数绑定的精确查找。
from __future__ import annotations
import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from .catalog import Catalog, Table

_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

@dataclass(frozen=True)
class EntityMatch:
    value: str
    table: str
    column: str
    rows: int
    entity_type: str


class ExactEntityLinker:
    """Exact-only value lookup over candidate text/key fields; values are bound params."""
    def __init__(self, catalog: Catalog, max_matches: int = 10):
        self.catalog = catalog
        self.max_matches = max_matches

    def link(self, question: str, candidate_refs: set[str]) -> list[EntityMatch]:
        patterns = self.catalog.ontology.get("identifier_patterns", [])
        values: list[tuple[str, str]] = []
        for item in patterns:
            try:
                pattern = re.compile(str(item["regex"]), re.I)
            except (KeyError, re.error):
                continue
            for match in pattern.finditer(question or ""):
                pair = (match.group(0), str(item.get("entity_type") or "unknown"))
                if pair not in values:
                    values.append(pair)
        matches: list[EntityMatch] = []
        for value, entity_type in values:
            binding = next((p for p in patterns if str(p.get("entity_type") or "unknown") == entity_type and p.get("table") and p.get("column")), None)
            for table in self.catalog.tables:
                if table.ref not in candidate_refs:
                    continue
                for column in table.columns:
                    if binding:
                        bound_table = str(binding["table"])
                        if "." not in bound_table:
                            bound_table = f"{binding.get('system_id', '')}.{bound_table}".strip(".")
                        if table.ref != bound_table or column.name != str(binding["column"]):
                            continue
                    if not (_SQL_IDENTIFIER.fullmatch(table.name) and _SQL_IDENTIFIER.fullmatch(column.name)):
                        continue
                    if column.primary_key or column.data_type.upper() in {"TEXT", "VARCHAR", "CHAR"}:
                        try:
                            with sqlite3.connect(f"file:{table.database_file}?mode=ro", uri=True, timeout=3) as con:
                                con.execute("PRAGMA query_only=ON")
                                row = con.execute(
                                    f'SELECT COUNT(*) FROM "{table.name}" WHERE CAST("{column.name}" AS TEXT) = ?',
                                    (value,),
                                ).fetchone()
                            count = int(row[0] or 0)
                        except sqlite3.Error:
                            continue
                        if count:
                            matches.append(EntityMatch(value, table.ref, column.name, count, entity_type))
                            if len(matches) >= self.max_matches:
                                return matches
        return matches

