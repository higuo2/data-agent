# 大目录：在 4 个核心系统上再生成约 26 个干扰系统、同名列和废弃表。只写元数据，不造业务行。
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE = ROOT.parent / "enterprise-data-agent-fixture"
CORE_DOMAINS = {
    "crm": "customer_master",
    "contracts": "contract_lifecycle",
    "sales": "order_management",
    "finance": "financials",
}
DISTRACTORS = [
    ("legacy_contracts", "contracts_archive", "已废弃历史合同库 archived signed contract headers"),
    ("hr", "human_resources", "人力资源员工档案与私人电话"),
    ("payroll", "human_resources", "薪酬个税与银行账号"),
    ("inventory", "supply_chain", "库存物料数量"),
    ("warehouse", "supply_chain", "仓储库位"),
    ("procurement", "supply_chain", "采购订单与供应商金额"),
    ("manufacturing", "supply_chain", "生产工单"),
    ("logistics", "supply_chain", "物流运单"),
    ("support", "service", "客服工单"),
    ("marketing", "go_to_market", "市场营销活动"),
    ("analytics_bi", "analytics", "分析宽表与废弃指标"),
    ("iot", "operations", "设备遥测"),
    ("billing_legacy", "finance_archive", "旧版计费账单金额 invoices"),
    ("tax", "finance_adjacent", "税务申报"),
    ("legal", "corporate", "法务案件"),
    ("facilities", "corporate", "行政设施"),
    ("projects", "delivery", "项目工时"),
    ("quality", "delivery", "质量缺陷"),
    ("partner_portal", "ecosystem", "合作伙伴门户"),
    ("ecommerce", "commerce", "电商订单金额 order_total"),
    ("pos", "commerce", "门店收银订单"),
    ("treasury", "finance_adjacent", "资金头寸"),
    ("risk", "control", "风险事件"),
    ("audit_log", "control", "审计日志"),
    ("knowledge_base", "service", "知识库文章"),
    ("field_service", "service", "现场服务工单"),
]
TABLE_NAMES = [
    "orders", "order_items", "customers", "invoices", "payments",
    "contract_header", "events", "snapshots", "notes", "allocations",
    "quotes", "refunds", "status_history", "approvals", "items",
    "balances", "fx_rates", "employees", "tickets", "metrics_old",
]
COLUMNS = (
    ("id", "TEXT", True),
    ("amount", "REAL", False),
    ("status", "TEXT", False),
    ("city", "TEXT", False),
    ("customer_id", "TEXT", False),
    ("order_id", "TEXT", False),
    ("signed_amount_tax_included", "REAL", False),
    ("order_total", "REAL", False),
)


def _columns() -> list[dict]:
    return [
        {
            "name": name,
            "type": dtype,
            "nullable": not pk,
            "primary_key": pk,
            "description": f"{name} collision column",
        }
        for name, dtype, pk in COLUMNS
    ]


def _distractor_table(system_id: str, domain: str, name: str) -> dict:
    deprecated = system_id == "legacy_contracts" and name in {"contract_header", "metrics_old"}
    if deprecated and name == "contract_header":
        description = "已废弃历史合同头表 signed contract amount; superseded, do not use"
    elif system_id == "ecommerce" and name == "orders":
        description = "电商订单金额 order_total，与签约合同无关"
    elif system_id == "hr" and name == "employees":
        description = "员工私人电话与档案，非合同或销售事实"
    else:
        description = f"{system_id} {name} 干扰表 with amount/status/city"
    return {
        "name": name,
        "description": description,
        "grain": f"one row per {name.rstrip('s')}",
        "domain": domain,
        "deprecated": deprecated,
        "columns": _columns(),
        "foreign_keys": [],
    }


def build_catalog(core: dict) -> dict:
    systems = []
    for item in core.get("systems") or []:
        sid = str(item.get("system_id") or "")
        copied = json.loads(json.dumps(item))
        copied["database_file"] = "data/dummy.sqlite"
        copied["domain"] = CORE_DOMAINS.get(sid, sid)
        for table in copied.get("tables") or []:
            table["domain"] = copied["domain"]
            table["deprecated"] = bool(table.get("deprecated"))
        systems.append(copied)
    for sid, domain, description in DISTRACTORS:
        systems.append({
            "system_id": sid,
            "description": description,
            "domain": domain,
            "database_file": "data/dummy.sqlite",
            "tables": [_distractor_table(sid, domain, name) for name in TABLE_NAMES],
        })
    return {
        "dataset": "enterprise-agent-large-catalog-v1",
        "systems": systems,
    }


def write_fixture(dest: Path, core_catalog: Path, ontology: Path) -> Path:
    dest = Path(dest)
    (dest / "benchmark").mkdir(parents=True, exist_ok=True)
    (dest / "data").mkdir(parents=True, exist_ok=True)
    core = json.loads(Path(core_catalog).read_text(encoding="utf-8"))
    catalog = build_catalog(core)
    (dest / "benchmark" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest / "benchmark" / "semantic_model.json").write_text(
        Path(ontology).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (dest / "data" / "dummy.sqlite").write_bytes(b"")
    return dest


if __name__ == "__main__":
    out = write_fixture(ROOT, CORE / "benchmark/catalog.json", CORE / "benchmark/semantic_model.json")
    catalog = json.loads((out / "benchmark/catalog.json").read_text(encoding="utf-8"))
    n_tables = sum(len(s.get("tables") or []) for s in catalog["systems"])
    print(f"Wrote {len(catalog['systems'])} systems, {n_tables} tables under {out}")
