# 目录加载：把目录和语义模型读成系统、表、列、外键；数据库路径必须留在测试数据根目录内。
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    description: str = ""
    primary_key: bool = False


@dataclass(frozen=True)
class Table:
    system_id: str
    database_file: Path
    name: str
    description: str
    grain: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[dict[str, Any], ...]

    @property
    def ref(self) -> str:
        return f"{self.system_id}.{self.name}"


@dataclass
class Catalog:
    systems: list[dict[str, Any]]
    tables: list[Table]
    ontology: dict[str, Any]
    root: Path

    @classmethod
    def load(cls, catalog_path: Path, ontology_path: Path) -> "Catalog":
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        ontology = json.loads(ontology_path.read_text(encoding="utf-8"))
        root = catalog_path.resolve().parent.parent
        systems = raw.get("systems")
        if not isinstance(systems, list) or not systems:
            raise ValueError("catalog must contain a non-empty systems array")
        tables: list[Table] = []
        for system in systems:
            sid = _required_string(system, "system_id")
            rel_db = _required_string(system, "database_file")
            db_path = (root / rel_db).resolve()
            if not db_path.is_relative_to(root.resolve()):
                raise ValueError(f"database file escapes fixture root: {rel_db}")
            for item in system.get("tables", []):
                name = _required_string(item, "name")
                columns = tuple(
                    Column(
                        name=_required_string(c, "name"),
                        data_type=str(c.get("type") or ""),
                        description=str(c.get("description") or ""),
                        primary_key=bool(c.get("primary_key")),
                    ) for c in item.get("columns", [])
                )
                if not columns:
                    raise ValueError(f"table {sid}.{name} has no columns")
                tables.append(Table(
                    system_id=sid,
                    database_file=db_path,
                    name=name,
                    description=str(item.get("description") or ""),
                    grain=str(item.get("grain") or ""),
                    columns=columns,
                    foreign_keys=tuple(item.get("foreign_keys") or []),
                ))
        return cls(systems=systems, tables=tables, ontology=ontology, root=root.resolve())


def _required_string(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required string: {key}")
    return value.strip()
