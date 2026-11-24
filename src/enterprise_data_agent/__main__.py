# 命令行入口：读入目录、语义模型和配置，把问题规划成只读查询；能执行才查库。
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .catalog import Catalog
from .planner import QueryPlanner


def main() -> None:
    p = argparse.ArgumentParser(description="Plan a read-only query from a data question")
    p.add_argument("--catalog", type=Path, required=True)
    p.add_argument("--ontology", type=Path, required=True)
    p.add_argument("--config", type=Path, default=Path("config/default.json"))
    p.add_argument("--question", required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    catalog = Catalog.load(args.catalog, args.ontology)
    result = QueryPlanner(catalog, config).plan(args.question, execute=True)
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
