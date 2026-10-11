"""
Configuration -> SQL metadata catalogue.

The version-controlled configuration is the source of truth:

    config/source_registry.yaml   sources, contracts, governance, load policy
    config/pipeline.yaml          mappings, handlers, dependencies, controls, lineage
    config/data_dictionary.yaml   business definitions of source columns
    config/business_rules.yaml    business-rule parameters
    config/posting_rules.csv      GL posting rules

`plan()` validates the configuration and builds the desired catalogue
without touching a database (used by CI). `sync()` applies it to the
meta schema idempotently: rows are inserted or updated, never deleted;
anything removed from configuration is disabled. Each sync is logged in
meta.MetadataSyncLog with the configuration checksum and git commit.
"""

import csv
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "src" / "validation"))

from common import load_registry_config  # noqa: E402
from handlers import HANDLERS, RECONCILIATION_HANDLERS, implementation_name  # noqa: E402
from staging_schema import StagingTable, all_staging_tables  # noqa: E402

CONFIG_DIR = ROOT / "config"
CONFIG_FILES = [
    "source_registry.yaml",
    "pipeline.yaml",
    "data_dictionary.yaml",
    "business_rules.yaml",
    "posting_rules.csv",
    "chart_of_accounts.csv",
]

TABLE_NAME = re.compile(r"^(stg|dw|mart)\.[A-Za-z][A-Za-z0-9_]*$")
SEVERITIES = {"CRITICAL", "ERROR", "WARNING"}
STRATEGIES = {"APPEND", "UPSERT", "SCD1", "SCD2", "FULL_REFRESH"}

DQ_DIMENSIONS = {
    "REQUIRED_VALUE": "Completeness",
    "UNIQUE": "Uniqueness",
    "DATA_TYPE": "Validity",
    "NUMERIC_RANGE": "Validity",
    "VALID_DATE": "Validity",
    "ALLOWED_VALUE": "Validity",
    "PATTERN": "Validity",
    "REFERENTIAL_INTEGRITY": "Consistency",
    "CALCULATION": "Accuracy",
    "COMPARISON": "Accuracy",
    "GROUP_BALANCE": "Accuracy",
}


class ConfigurationError(RuntimeError):
    """The configuration is inconsistent; the message lists every problem."""


@dataclass
class Catalogue:
    sources: list[dict] = field(default_factory=list)
    elements: list[dict] = field(default_factory=list)
    handlers: list[dict] = field(default_factory=list)
    mappings: list[dict] = field(default_factory=list)
    dependencies: list[tuple[str, str]] = field(default_factory=list)
    controls: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    lineage: list[dict] = field(default_factory=list)
    settings: list[dict] = field(default_factory=list)
    posting_rules: list[dict] = field(default_factory=list)
    rebate_thresholds: list[dict] = field(default_factory=list)
    checksum: str = ""


# ---------------------------------------------------------
# LOADING
# ---------------------------------------------------------


def load_yaml(name: str) -> dict:

    with open(CONFIG_DIR / name, encoding="utf-8") as file:
        return yaml.safe_load(file)


def config_checksum() -> str:

    digest = hashlib.sha256()

    for name in CONFIG_FILES:
        digest.update(name.encode())
        digest.update((CONFIG_DIR / name).read_bytes().replace(b"\r\n", b"\n"))

    return digest.hexdigest()


def git_commit() -> str | None:

    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            or None
        )
    except Exception:
        return None


def staging_code(definition_table: str) -> str:
    """stg.SalesTransaction -> STG_SALES_TRANSACTION"""

    name = definition_table.split(".", 1)[1]

    return "STG_" + re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


# ---------------------------------------------------------
# PLAN (no database)
# ---------------------------------------------------------


def plan() -> Catalogue:
    """Validate the configuration and build the desired catalogue."""

    registry = load_registry_config()
    pipeline = load_yaml("pipeline.yaml")
    dictionary = load_yaml("data_dictionary.yaml")
    business_rules = load_yaml("business_rules.yaml")

    problems = []
    catalogue = Catalogue(checksum=config_checksum())

    critical = set((registry.get("load_policy") or {}).get("critical_sources") or [])
    freshness = (pipeline.get("pipeline") or {}).get("freshness_hours") or {}

    # ---------------- sources
    staging_tables = {t.source: t for t in all_staging_tables(registry)}

    for name, config in registry["sources"].items():
        governance = config.get("governance") or {}
        frequency = governance.get("update_frequency")

        if frequency and frequency not in freshness:
            problems.append(f"Source {name}: update_frequency {frequency!r} has no freshness_hours entry.")

        catalogue.sources.append(
            {
                "SourceName": name,
                "SourceType": {"xlsx": "Excel", "csv": "CSV", "pdf": "PDF"}.get(config["format"], config["format"]),
                "Description": governance.get("description"),
                "FileFormat": config["format"],
                "PathPattern": config["path"],
                "StagingTable": config.get("staging_table"),
                "SystemOfRecord": governance.get("system_of_record"),
                "DataOwner": governance.get("data_owner"),
                "DataSteward": governance.get("data_steward"),
                "Classification": (governance.get("classification") or "").upper() or None,
                "ContainsPersonalData": bool(governance.get("contains_personal_data")),
                "RetentionYears": governance.get("retention_years"),
                "UpdateFrequency": frequency,
                "FreshnessThresholdHours": freshness.get(frequency),
                "IsCritical": name in critical,
            }
        )

    for name, config in (pipeline.get("reference_sources") or {}).items():
        catalogue.sources.append(
            {
                "SourceName": name,
                "SourceType": config["type"],
                "Description": config.get("description"),
                "FileFormat": "csv" if config.get("path", "").endswith(".csv") else None,
                "PathPattern": config.get("path"),
                "StagingTable": config.get("staging_table"),
                "SystemOfRecord": "Configuration repository" if config["type"] == "Reference" else "Data platform",
                "DataOwner": config.get("data_owner"),
                "DataSteward": config.get("data_steward"),
                "Classification": (config.get("classification") or "").upper() or None,
                "ContainsPersonalData": False,
                "RetentionYears": None,
                "UpdateFrequency": "On change",
                "FreshnessThresholdHours": None,
                "IsCritical": name == "chart_of_accounts",
            }
        )

    source_names = {s["SourceName"] for s in catalogue.sources}

    # ---------------- data dictionary (elements)
    for source, definition in staging_tables.items():
        catalogue.elements += source_elements(source, registry["sources"][source], definition, dictionary, problems)

    for column, text in ((dictionary.get("reference") or {}).get("chart_of_accounts") or {}).items():
        catalogue.elements.append(
            {
                "SourceName": "chart_of_accounts",
                "EntityName": "ChartOfAccounts",
                "Domain": "Finance",
                "EntityDefinition": "Chart of accounts reference data.",
                "ElementName": column,
                "BusinessDefinition": text,
                "DataType": "NVARCHAR",
                "IsNullable": False,
                "IsBusinessKey": column == "account_code",
                "DataClassification": "INTERNAL",
                "ValidationRule": "Required" + ("; unique" if column == "account_code" else ""),
                "ColumnOrder": len([e for e in catalogue.elements if e["SourceName"] == "chart_of_accounts"]) + 1,
                "ContainsPersonalData": False,
                "StagingColumn": f"stg.ChartOfAccounts.{column}",
            }
        )

    # ---------------- handlers
    configured_handlers = pipeline.get("handlers") or {}

    for code, config in configured_handlers.items():
        implemented = code in HANDLERS or code in RECONCILIATION_HANDLERS

        if not implemented:
            problems.append(f"Handler {code} is configured but has no implementation in handlers.py.")
            continue

        expected_type = "RECONCILIATION" if code in RECONCILIATION_HANDLERS else HANDLERS[code].handler_type

        if config.get("type") != expected_type:
            problems.append(
                f"Handler {code}: type {config.get('type')!r} does not match implementation {expected_type!r}."
            )

        catalogue.handlers.append(
            {
                "HandlerCode": code,
                "HandlerType": expected_type,
                "Description": config.get("description"),
                "IsApproved": bool(config.get("approved")),
                "Implementation": implementation_name(code),
            }
        )

    # ---------------- mappings
    overrides = pipeline.get("staging") or {}
    default_controls = pipeline.get("default_controls") or {}

    staging_mappings = []

    for source, definition in staging_tables.items():
        override = overrides.get(source) or {}
        staging_mappings.append(
            {
                "code": override.get("code", staging_code(definition.qualified_name)),
                "source": source,
                "target": definition.qualified_name,
                "layer": "STAGING",
                "strategy": "APPEND",
                "handler": override.get("handler", "GENERIC_STAGING"),
                "order": 10,
                "depends_on": [],
                "description": f"Validated {source} files into {definition.qualified_name}.",
            }
        )

    for source, config in (pipeline.get("reference_sources") or {}).items():
        if config.get("staging_table"):
            override = overrides.get(source) or {}
            staging_mappings.append(
                {
                    "code": override.get("code", staging_code(config["staging_table"])),
                    "source": source,
                    "target": config["staging_table"],
                    "layer": "STAGING",
                    "strategy": "APPEND",
                    "handler": override["handler"],
                    "order": 10,
                    "depends_on": [],
                    "description": f"{config['path']} into {config['staging_table']}.",
                }
            )

    warehouse_mappings = [
        {**m, "layer": m.get("layer", "WAREHOUSE"), "depends_on": m.get("depends_on") or []}
        for m in pipeline.get("mappings") or []
    ]

    all_mappings = staging_mappings + warehouse_mappings
    codes = [m["code"] for m in all_mappings]

    for code in {c for c in codes if codes.count(c) > 1}:
        problems.append(f"Mapping code {code} is defined more than once.")

    targets = [m["target"] for m in all_mappings]

    for target in {t for t in targets if targets.count(t) > 1}:
        problems.append(f"Target {target} is written by more than one mapping.")

    for mapping in all_mappings:
        label = mapping["code"]

        if mapping["source"] not in source_names:
            problems.append(f"Mapping {label}: unknown source {mapping['source']!r}.")

        if not TABLE_NAME.match(mapping["target"]):
            problems.append(f"Mapping {label}: target {mapping['target']!r} is not schema.Table in stg/dw/mart.")

        if mapping["strategy"] not in STRATEGIES:
            problems.append(f"Mapping {label}: unknown strategy {mapping['strategy']!r}.")

        handler_code = mapping["handler"]
        handler = HANDLERS.get(handler_code)

        if handler is None:
            problems.append(f"Mapping {label}: handler {handler_code} is not an approved execution handler.")
        else:
            if mapping["layer"] not in handler.layers:
                problems.append(f"Mapping {label}: handler {handler_code} may not run in layer {mapping['layer']}.")
            if handler.target and handler.target != mapping["target"]:
                problems.append(f"Mapping {label}: handler {handler_code} may only write {handler.target}.")
            if handler_code not in configured_handlers or not configured_handlers[handler_code].get("approved"):
                problems.append(f"Mapping {label}: handler {handler_code} is not approved in pipeline.yaml.")

        for parent in mapping["depends_on"]:
            if parent not in codes:
                problems.append(f"Mapping {label}: depends on unknown mapping {parent}.")
            else:
                catalogue.dependencies.append((label, parent))

        if mapping["layer"] in ("WAREHOUSE", "MART") and not mapping.get("columns"):
            problems.append(f"Mapping {label}: warehouse mappings must document column lineage (columns).")

        catalogue.mappings.append(
            {
                "MappingCode": label,
                "SourceName": mapping["source"],
                "TargetSchema": mapping["target"].split(".")[0],
                "TargetTable": mapping["target"].split(".")[1],
                "LoadLayer": mapping["layer"],
                "LoadStrategy": mapping["strategy"],
                "ExecutionOrder": mapping["order"],
                "Description": mapping.get("description"),
                "MaxRetries": mapping.get("max_retries", 1),
                "RetryDelaySeconds": mapping.get("retry_delay_seconds", 5),
                "HandlerCode": handler_code,
            }
        )

        # ---------------- controls
        skipped = set(mapping.get("skip_default_controls") or [])
        controls = [c for c in default_controls.get(mapping["layer"], []) if c["name"] not in skipped]
        controls += mapping.get("controls") or []

        for control in controls:
            code = f"{label}.{control['name']}"

            if control["handler"] not in RECONCILIATION_HANDLERS:
                problems.append(f"Control {code}: {control['handler']} is not a reconciliation handler.")

            if control.get("severity") not in SEVERITIES:
                problems.append(f"Control {code}: severity must be one of {sorted(SEVERITIES)}.")

            if control.get("tolerance") not in (None, 0) and not control.get("tolerance_reason"):
                problems.append(f"Control {code}: a non-zero tolerance needs a tolerance_reason.")

            catalogue.controls.append(
                {
                    "ControlCode": code,
                    "MappingCode": label,
                    "HandlerCode": control["handler"],
                    "Severity": control.get("severity"),
                    "IsMandatory": control.get("mandatory", True),
                    "Tolerance": control.get("tolerance"),
                    "ToleranceType": control.get("tolerance_type", "ABSOLUTE")
                    if control.get("tolerance") is not None
                    else None,
                    "ParametersJson": json.dumps(control.get("params")) if control.get("params") else None,
                    "Description": (
                        control.get("tolerance_reason") and f"Tolerance justified: {control['tolerance_reason']}"
                    )
                    or None,
                }
            )

        # ---------------- lineage
        if mapping["layer"] == "STAGING":
            definition = staging_tables.get(mapping["source"])
            for column in definition.column_names if definition else []:
                catalogue.lineage.append(
                    {
                        "MappingCode": label,
                        "TargetObject": mapping["target"],
                        "TargetColumn": column,
                        "SourceName": mapping["source"],
                        "SourceObject": registry["sources"][mapping["source"]]["path"],
                        "SourceColumns": column,
                        "TransformationRule": "Validated value converted to the staging type (no business logic).",
                    }
                )
        else:
            for column, spec in (mapping.get("columns") or {}).items():
                catalogue.lineage.append(
                    {
                        "MappingCode": label,
                        "TargetObject": mapping["target"],
                        "TargetColumn": column,
                        "SourceName": mapping["source"],
                        "SourceObject": staging_source_object(mapping, registry, pipeline),
                        "SourceColumns": str(spec.get("from")) if spec.get("from") else None,
                        "TransformationRule": str(spec.get("rule")),
                    }
                )

    for target, spec in (pipeline.get("static_tables") or {}).items():
        for column, column_spec in (spec.get("columns") or {}).items():
            catalogue.lineage.append(
                {
                    "MappingCode": "STATIC",
                    "TargetObject": target,
                    "TargetColumn": column,
                    "SourceName": None,
                    "SourceObject": spec.get("seeded_by"),
                    "SourceColumns": str(column_spec["from"]) if column_spec.get("from") else None,
                    "TransformationRule": str(column_spec.get("rule")),
                }
            )

    problems += dependency_cycles(catalogue.dependencies)

    # ---------------- business rules catalogue (validation contracts)
    for source, config in registry["sources"].items():
        catalogue.rules += source_rules(source, config)

    # ---------------- settings, posting rules, rebate thresholds
    catalogue.settings = flatten_settings(business_rules)

    with open(CONFIG_DIR / "posting_rules.csv", newline="", encoding="utf-8-sig") as file:
        catalogue.posting_rules = [
            {
                "TransactionType": r["transaction_type"],
                "DebitAccount": r["debit_account"] or None,
                "CreditAccount": r["credit_account"] or None,
                "Description": r["description"],
            }
            for r in csv.DictReader(file)
        ]

    rebates = (business_rules.get("operations") or {}).get("rebates") or {}
    catalogue.rebate_thresholds = [
        {"ThresholdNAD": t["threshold"], "RebateRate": t["rate"]} for t in rebates.get("quarterly_thresholds") or []
    ]

    if problems:
        raise ConfigurationError("Configuration problems:\n  - " + "\n  - ".join(problems))

    return catalogue


def staging_source_object(mapping: dict, registry: dict, pipeline: dict) -> str:

    source = mapping["source"]

    if source in registry["sources"]:
        return registry["sources"][source].get("staging_table") or source

    return ((pipeline.get("reference_sources") or {}).get(source) or {}).get("staging_table") or source


def source_elements(
    source: str, config: dict, definition: StagingTable, dictionary: dict, problems: list
) -> list[dict]:

    governance = config.get("governance") or {}
    rules = config.get("data_rules") or {}
    personal = set(governance.get("personal_data_fields") or [])
    extraction = (config.get("extraction") or {}).get("fields") or {}
    label_to_field = {spec["label"]: name for name, spec in extraction.items()}
    personal_fields = {label_to_field.get(p, p) for p in personal}
    classification = (governance.get("classification") or "INTERNAL").upper()

    definitions = {**(dictionary.get("common") or {}), **((dictionary.get("sources") or {}).get(source) or {})}
    domain = (dictionary.get("domains") or {}).get(source)

    elements = []

    for order, column in enumerate(definition.columns, start=1):
        text = definitions.get(column.name)

        if not text:
            problems.append(f"Data dictionary: no definition for {source}.{column.name}.")
            text = "(definition missing)"

        elements.append(
            {
                "SourceName": source,
                "EntityName": definition.table,
                "Domain": domain,
                "EntityDefinition": governance.get("description") or source,
                "ElementName": column.name,
                "BusinessDefinition": text,
                "DataType": column.sql_type,
                "IsNullable": column.nullable,
                "IsBusinessKey": column.name in definition.business_key,
                "DataClassification": classification
                if column.name in personal_fields
                else ("INTERNAL" if classification == "PUBLIC" else classification),
                "ValidationRule": describe_column_rules(column.name, rules, config.get("business_rules") or {}),
                "ColumnOrder": order,
                "ContainsPersonalData": column.name in personal_fields,
                "StagingColumn": f"{definition.qualified_name}.{column.name}",
            }
        )

    return elements


def describe_column_rules(column: str, rules: dict, business: dict) -> str | None:

    parts = []

    if column in (rules.get("required_values") or []):
        parts.append("Required")

    for key in rules.get("unique") or []:
        fields = [key] if isinstance(key, str) else key
        if column in fields:
            parts.append("Unique" if len(fields) == 1 else f"Unique with {'+'.join(f for f in fields if f != column)}")

    numeric = (rules.get("numeric") or {}).get(column)
    if numeric is not None:
        limits = numeric or {}
        text = "Whole number" if limits.get("integer") else "Number"
        if limits.get("minimum") is not None:
            text += f" >= {limits['minimum']}"
        if limits.get("maximum") is not None:
            text += f" <= {limits['maximum']}"
        parts.append(text)

    if column in (rules.get("dates") or []):
        parts.append("Valid date")

    if column in (rules.get("allowed_values") or {}):
        parts.append(f"One of {rules['allowed_values'][column]}")

    if column in (rules.get("patterns") or {}):
        parts.append(f"Matches {rules['patterns'][column]}")

    reference = (rules.get("references") or {}).get(column)
    if reference:
        parts.append(f"Must exist in {reference.get('source') or reference.get('config')}.{reference['field']}")

    for calculation in business.get("calculations") or []:
        if calculation["field"] == column:
            parts.append(f"= {calculation['formula']}")

    return "; ".join(parts) or None


def source_rules(source: str, config: dict) -> list[dict]:
    """The validation contract as a business-rule catalogue."""

    governance = config.get("governance") or {}
    owner = governance.get("data_owner")
    rules = config.get("data_rules") or {}
    business = config.get("business_rules") or {}
    catalogue = []

    def add(rule_type, field_name, definition, severity="ERROR", parameters=None, stage="DATA_VALIDATION"):
        catalogue.append(
            {
                "RuleCode": f"{source}.{rule_type}.{field_name or 'FILE'}",
                "SourceName": source,
                "RuleStage": stage,
                "RuleType": rule_type,
                "FieldName": field_name,
                "Severity": severity,
                "Definition": definition,
                "ParametersJson": json.dumps(parameters, default=str) if parameters else None,
                "RuleOwner": owner,
                "DqDimension": DQ_DIMENSIONS.get(rule_type),
            }
        )

    for column in rules.get("required_values") or []:
        add("REQUIRED_VALUE", column, f"{column} must not be blank.")

    for key in rules.get("unique") or []:
        fields = [key] if isinstance(key, str) else key
        add("UNIQUE", "+".join(fields), f"{' + '.join(fields)} must be unique.")

    for column, limits in (rules.get("numeric") or {}).items():
        add(
            "DATA_TYPE", column, f"{column} must be {'a whole number' if (limits or {}).get('integer') else 'numeric'}."
        )
        if limits and (limits.get("minimum") is not None or limits.get("maximum") is not None):
            add("NUMERIC_RANGE", column, f"{column} must be within the permitted range.", parameters=limits)

    for column in rules.get("dates") or []:
        add("VALID_DATE", column, f"{column} must be a valid date.")

    for column, allowed in (rules.get("allowed_values") or {}).items():
        add("ALLOWED_VALUE", column, f"{column} must be one of {allowed}.", parameters={"allowed": allowed})

    for column, pattern in (rules.get("patterns") or {}).items():
        add("PATTERN", column, f"{column} must match {pattern}.", parameters={"pattern": pattern})

    for column, reference in (rules.get("references") or {}).items():
        target = f"{reference.get('source') or reference.get('config')}.{reference['field']}"
        add(
            "REFERENTIAL_INTEGRITY",
            column,
            f"{column} must exist in {target}.",
            severity=reference.get("severity", "ERROR"),
            parameters=reference,
        )

    for calculation in business.get("calculations") or []:
        add(
            calculation.get("name", "CALCULATION"),
            calculation["field"],
            f"{calculation['field']} must equal {calculation['formula']}.",
            severity=calculation.get("severity", "ERROR"),
            parameters=calculation,
            stage="BUSINESS_RULE_VALIDATION",
        )
        catalogue[-1]["DqDimension"] = "Accuracy"

    for comparison in business.get("comparisons") or []:
        right = (
            comparison.get("other_field")
            or comparison.get("value")
            or f"{comparison['lookup']['source']}.{comparison['lookup']['field']}"
        )
        add(
            comparison.get("name", "COMPARISON"),
            comparison["field"],
            f"{comparison['field']} must be {comparison['operator']} {right}.",
            severity=comparison.get("severity", "ERROR"),
            parameters=comparison,
            stage="BUSINESS_RULE_VALIDATION",
        )
        catalogue[-1]["DqDimension"] = "Accuracy"

    for balance in business.get("group_balance") or []:
        add(
            balance.get("name", "GROUP_BALANCE"),
            balance["group_by"],
            f"Debits must equal credits per {balance['group_by']}.",
            severity=balance.get("severity", "ERROR"),
            parameters=balance,
            stage="BUSINESS_RULE_VALIDATION",
        )
        catalogue[-1]["DqDimension"] = "Accuracy"

    return catalogue


def flatten_settings(business_rules: dict, prefix: str = "") -> list[dict]:

    settings = []

    for key, value in business_rules.items():
        name = f"{prefix}{key}"

        if isinstance(value, dict):
            settings += flatten_settings(value, f"{name}.")
        elif isinstance(value, bool):
            settings.append({"SettingKey": name, "SettingValue": str(value).lower(), "ValueType": "BOOL"})
        elif isinstance(value, int):
            settings.append({"SettingKey": name, "SettingValue": str(value), "ValueType": "INT"})
        elif isinstance(value, float):
            settings.append({"SettingKey": name, "SettingValue": str(value), "ValueType": "DECIMAL"})
        elif isinstance(value, str):
            settings.append({"SettingKey": name, "SettingValue": value, "ValueType": "TEXT"})

    return settings


def dependency_cycles(dependencies: list[tuple[str, str]]) -> list[str]:

    graph = {}

    for child, parent in dependencies:
        graph.setdefault(child, set()).add(parent)

    problems, visiting, done = [], set(), set()

    def visit(node, path):
        if node in done:
            return
        if node in visiting:
            problems.append(f"Circular mapping dependency: {' -> '.join(path + [node])}")
            return
        visiting.add(node)
        for parent in graph.get(node, ()):
            visit(parent, path + [node])
        visiting.discard(node)
        done.add(node)

    for node in list(graph):
        visit(node, [])

    return problems


# ---------------------------------------------------------
# SYNC (database)
# ---------------------------------------------------------


def sync(connection, catalogue: Catalogue) -> dict:
    """Apply the catalogue to the meta schema. Returns counts."""

    cursor = connection.cursor()
    counts = {"inserted": 0, "updated": 0, "disabled": 0}

    def upsert(table, key_columns, row, extra_update=""):
        keys = " AND ".join(f"[{k}] = ?" for k in key_columns)
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {keys}", *[row[k] for k in key_columns])
        exists = cursor.fetchone()[0]
        columns = list(row)
        if exists:
            assignments = ", ".join(f"[{c}] = ?" for c in columns if c not in key_columns)
            cursor.execute(
                f"UPDATE {table} SET {assignments}{extra_update} WHERE {keys}",
                *[row[c] for c in columns if c not in key_columns],
                *[row[k] for k in key_columns],
            )
            counts["updated"] += 1
        else:
            cursor.execute(
                f"INSERT INTO {table} ({', '.join(f'[{c}]' for c in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                *[row[c] for c in columns],
            )
            counts["inserted"] += 1

    # Sources
    for source in catalogue.sources:
        upsert("meta.DataSource", ["SourceName"], {**source, "IsActive": 1}, ", UpdatedAt = SYSUTCDATETIME()")

    source_ids = dict(cursor.execute("SELECT SourceName, SourceID FROM meta.DataSource").fetchall())

    # Entities and elements
    for element in catalogue.elements:
        upsert(
            "meta.DataEntity",
            ["SourceID", "EntityName"],
            {
                "SourceID": source_ids[element["SourceName"]],
                "EntityName": element["EntityName"],
                "BusinessDefinition": element["EntityDefinition"],
                "BusinessDomain": element["Domain"],
            },
        )

    entity_ids = {
        (row.SourceID, row.EntityName): row.EntityID
        for row in cursor.execute("SELECT EntityID, SourceID, EntityName FROM meta.DataEntity").fetchall()
    }

    for element in catalogue.elements:
        upsert(
            "meta.DataElement",
            ["EntityID", "ElementName"],
            {
                "EntityID": entity_ids[(source_ids[element["SourceName"]], element["EntityName"])],
                "ElementName": element["ElementName"],
                "BusinessDefinition": element["BusinessDefinition"],
                "DataType": element["DataType"],
                "IsNullable": element["IsNullable"],
                "IsBusinessKey": element["IsBusinessKey"],
                "DataClassification": element["DataClassification"],
                "ValidationRule": element["ValidationRule"],
                "ColumnOrder": element["ColumnOrder"],
                "ContainsPersonalData": element["ContainsPersonalData"],
                "StagingColumn": element["StagingColumn"],
            },
            ", UpdatedAt = SYSUTCDATETIME()",
        )

    # Handlers
    configured = {h["HandlerCode"] for h in catalogue.handlers}
    for handler in catalogue.handlers:
        upsert("meta.ExecutionHandler", ["HandlerCode"], {**handler, "IsActive": 1})
    for (code,) in cursor.execute("SELECT HandlerCode FROM meta.ExecutionHandler WHERE IsActive = 1").fetchall():
        if code not in configured:
            cursor.execute("UPDATE meta.ExecutionHandler SET IsActive = 0 WHERE HandlerCode = ?", code)
            counts["disabled"] += 1

    handler_ids = dict(cursor.execute("SELECT HandlerCode, HandlerID FROM meta.ExecutionHandler").fetchall())

    # Mappings (matched by code, or by source + target for rows created before codes existed)
    configured_codes = set()

    for mapping in catalogue.mappings:
        configured_codes.add(mapping["MappingCode"])
        source_id = source_ids[mapping["SourceName"]]

        cursor.execute(
            "SELECT MappingID FROM meta.SourceTargetMapping WHERE MappingCode = ? "
            "OR (MappingCode IS NULL AND SourceID = ? AND TargetSchema = ? AND TargetTable = ?)",
            mapping["MappingCode"],
            source_id,
            mapping["TargetSchema"],
            mapping["TargetTable"],
        )
        existing = cursor.fetchone()

        values = (
            source_id,
            mapping["TargetSchema"],
            mapping["TargetTable"],
            mapping["LoadLayer"],
            mapping["LoadStrategy"],
            mapping["ExecutionOrder"],
            mapping["Description"],
            mapping["MaxRetries"],
            mapping["RetryDelaySeconds"],
            mapping["MappingCode"],
        )

        if existing:
            cursor.execute(
                "UPDATE meta.SourceTargetMapping SET SourceID = ?, TargetSchema = ?, TargetTable = ?, "
                "LoadLayer = ?, LoadStrategy = ?, ExecutionOrder = ?, Description = ?, MaxRetries = ?, "
                "RetryDelaySeconds = ?, MappingCode = ?, IsEnabled = 1, UpdatedAt = SYSUTCDATETIME() "
                "WHERE MappingID = ?",
                *values,
                existing[0],
            )
            counts["updated"] += 1
        else:
            cursor.execute(
                "INSERT INTO meta.SourceTargetMapping (SourceID, TargetSchema, TargetTable, LoadLayer, "
                "LoadStrategy, ExecutionOrder, Description, MaxRetries, RetryDelaySeconds, MappingCode, "
                "IsEnabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                *values,
            )
            counts["inserted"] += 1

    for mapping_id, code in cursor.execute(
        "SELECT MappingID, MappingCode FROM meta.SourceTargetMapping WHERE IsEnabled = 1"
    ).fetchall():
        if code not in configured_codes:
            cursor.execute("UPDATE meta.SourceTargetMapping SET IsEnabled = 0 WHERE MappingID = ?", mapping_id)
            counts["disabled"] += 1

    mapping_ids = dict(
        cursor.execute(
            "SELECT MappingCode, MappingID FROM meta.SourceTargetMapping WHERE MappingCode IS NOT NULL"
        ).fetchall()
    )

    for mapping in catalogue.mappings:
        upsert(
            "meta.MappingExecution",
            ["MappingID"],
            {
                "MappingID": mapping_ids[mapping["MappingCode"]],
                "HandlerID": handler_ids[mapping["HandlerCode"]],
                "StopOnFailure": 1,
            },
        )

    # Dependencies: exactly as configured for configured mappings
    desired = {(mapping_ids[c], mapping_ids[p]) for c, p in catalogue.dependencies}
    current = {
        (int(a), int(b))
        for a, b in cursor.execute("SELECT MappingID, DependsOnMappingID FROM meta.MappingDependency").fetchall()
    }
    configured_ids = {mapping_ids[c] for c in configured_codes}

    for child, parent in desired - current:
        cursor.execute(
            "INSERT INTO meta.MappingDependency (MappingID, DependsOnMappingID) VALUES (?, ?)", child, parent
        )
        counts["inserted"] += 1

    for child, parent in current - desired:
        if child in configured_ids:
            cursor.execute(
                "DELETE FROM meta.MappingDependency WHERE MappingID = ? AND DependsOnMappingID = ?", child, parent
            )
            counts["disabled"] += 1

    # Reconciliation controls
    configured_controls = set()
    for control in catalogue.controls:
        configured_controls.add(control["ControlCode"])
        upsert(
            "meta.ReconciliationControl",
            ["ControlCode"],
            {
                "ControlCode": control["ControlCode"],
                "MappingID": mapping_ids[control["MappingCode"]],
                "HandlerID": handler_ids[control["HandlerCode"]],
                "Severity": control["Severity"],
                "IsMandatory": control["IsMandatory"],
                "Tolerance": control["Tolerance"],
                "ToleranceType": control["ToleranceType"],
                "ParametersJson": control["ParametersJson"],
                "Description": control["Description"],
                "IsEnabled": 1,
            },
            ", UpdatedAt = SYSUTCDATETIME()",
        )

    for (code,) in cursor.execute("SELECT ControlCode FROM meta.ReconciliationControl WHERE IsEnabled = 1").fetchall():
        if code not in configured_controls:
            cursor.execute("UPDATE meta.ReconciliationControl SET IsEnabled = 0 WHERE ControlCode = ?", code)
            counts["disabled"] += 1

    # Business rules, lineage, settings, posting rules, rebate thresholds
    configured_rules = set()
    for rule in catalogue.rules:
        configured_rules.add(rule["RuleCode"])
        upsert("meta.BusinessRule", ["RuleCode"], {**rule, "IsActive": 1}, ", UpdatedAt = SYSUTCDATETIME()")
    for (code,) in cursor.execute("SELECT RuleCode FROM meta.BusinessRule WHERE IsActive = 1").fetchall():
        if code not in configured_rules:
            cursor.execute("UPDATE meta.BusinessRule SET IsActive = 0 WHERE RuleCode = ?", code)
            counts["disabled"] += 1

    for row in catalogue.lineage:
        upsert("meta.ColumnLineage", ["TargetObject", "TargetColumn"], row, ", UpdatedAt = SYSUTCDATETIME()")

    for setting in catalogue.settings:
        upsert("meta.Setting", ["SettingKey"], setting, ", UpdatedAt = SYSUTCDATETIME()")

    for rule in catalogue.posting_rules:
        upsert("meta.PostingRule", ["TransactionType"], rule, ", UpdatedAt = SYSUTCDATETIME()")

    cursor.execute("DELETE FROM meta.RebateThreshold")
    for threshold in catalogue.rebate_thresholds:
        cursor.execute(
            "INSERT INTO meta.RebateThreshold (ThresholdNAD, RebateRate) VALUES (?, ?)",
            threshold["ThresholdNAD"],
            threshold["RebateRate"],
        )

    cursor.execute(
        "INSERT INTO meta.MetadataSyncLog (ConfigChecksum, GitCommit, ItemsInserted, ItemsUpdated, "
        "ItemsDisabled, Summary) VALUES (?, ?, ?, ?, ?, ?)",
        catalogue.checksum,
        git_commit(),
        counts["inserted"],
        counts["updated"],
        counts["disabled"],
        (
            f"{len(catalogue.sources)} sources, {len(catalogue.elements)} elements, "
            f"{len(catalogue.mappings)} mappings, {len(catalogue.controls)} controls, "
            f"{len(catalogue.rules)} business rules, {len(catalogue.lineage)} lineage entries"
        ),
    )

    connection.commit()

    return counts
