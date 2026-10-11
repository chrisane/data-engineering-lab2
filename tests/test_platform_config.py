"""
Configuration and metadata tests (no database needed).

Prove that the pipeline configuration is consistent: every handler is
implemented and approved, targets match handlers, dependencies resolve
without cycles, controls are well-formed, every source column has a
business definition and warehouse mappings document their lineage.
"""

import copy

import pytest
import yaml

import metadata_sync
from handlers import HANDLERS, RECONCILIATION_HANDLERS
from metadata_sync import ConfigurationError, dependency_cycles, flatten_settings, plan, staging_code
from reconciliation import CONTROLS


@pytest.fixture(scope="module")
def catalogue():
    return plan()


def test_configuration_is_valid(catalogue):

    assert catalogue.mappings
    assert catalogue.controls
    assert len(catalogue.checksum) == 64


def test_every_registered_source_has_a_staging_mapping(catalogue):

    staged = {m["SourceName"] for m in catalogue.mappings if m["LoadLayer"] == "STAGING"}
    registry = metadata_sync.load_registry_config()

    assert set(registry["sources"]) <= staged


def test_every_source_with_a_warehouse_target(catalogue):
    """Phase C: every registered source reaches the warehouse."""

    warehoused = {m["SourceName"] for m in catalogue.mappings if m["LoadLayer"] == "WAREHOUSE"}
    registry = metadata_sync.load_registry_config()

    assert set(registry["sources"]) - warehoused == set()


def test_handlers_in_configuration_and_code_agree(catalogue):

    configured = {h["HandlerCode"] for h in catalogue.handlers}

    assert configured == set(HANDLERS) | RECONCILIATION_HANDLERS
    assert set(CONTROLS) == RECONCILIATION_HANDLERS


def test_every_warehouse_column_has_lineage(catalogue):

    warehouse = {(m["TargetSchema"], m["TargetTable"]) for m in catalogue.mappings if m["LoadLayer"] == "WAREHOUSE"}
    documented = {tuple(entry["TargetObject"].split(".")) for entry in catalogue.lineage}

    assert warehouse <= documented


def test_every_data_element_has_a_definition(catalogue):

    assert all(e["BusinessDefinition"] != "(definition missing)" for e in catalogue.elements)


def test_personal_data_is_classified(catalogue):

    personal = [e for e in catalogue.elements if e["ContainsPersonalData"]]

    assert {(e["SourceName"], e["ElementName"]) for e in personal} >= {
        ("customer_master", "customer_name"),
        ("branch_local_assets", "assigned_to"),
        ("branch_cashup", "cashier"),
    }
    assert all(e["DataClassification"] in ("CONFIDENTIAL", "RESTRICTED") for e in personal)


def test_non_zero_tolerances_are_justified(catalogue):

    for control in catalogue.controls:
        if control["Tolerance"] not in (None, 0):
            assert control["Description"], control["ControlCode"]


def test_settings_include_parameters_used_by_sql(catalogue):

    keys = {s["SettingKey"] for s in catalogue.settings}

    assert {
        "reporting.as_at_date",
        "cashup.variance_tolerance_nad",
        "replenishment.safety_stock_days",
        "company.financial_year_start_month",
        "purchasing.invoice_match_tolerance_nad",
    } <= keys


def test_dependency_cycles_are_detected():

    assert dependency_cycles([("A", "B"), ("B", "C")]) == []
    assert dependency_cycles([("A", "B"), ("B", "A")])


def test_staging_codes():

    assert staging_code("stg.SalesTransaction") == "STG_SALES_TRANSACTION"
    assert staging_code("stg.Site") == "STG_SITE"


def test_flatten_settings_types():

    settings = {s["SettingKey"]: s for s in flatten_settings({"a": {"b": 1, "c": 0.5, "d": True, "e": "x", "f": [1]}})}

    assert settings["a.b"]["ValueType"] == "INT"
    assert settings["a.c"]["ValueType"] == "DECIMAL"
    assert settings["a.d"]["SettingValue"] == "true"
    assert "a.f" not in settings


# ---------------------------------------------------------
# Negative tests: a broken configuration is rejected
# ---------------------------------------------------------


@pytest.fixture
def broken_pipeline(monkeypatch):
    """Patch load_yaml so tests can edit pipeline.yaml in memory."""

    original = metadata_sync.load_yaml
    pipeline = copy.deepcopy(original("pipeline.yaml"))

    def fake_load(name):
        return pipeline if name == "pipeline.yaml" else original(name)

    monkeypatch.setattr(metadata_sync, "load_yaml", fake_load)
    return pipeline


def test_unknown_handler_is_rejected(broken_pipeline):

    broken_pipeline["mappings"][0]["handler"] = "RUN_ANY_SQL"

    with pytest.raises(ConfigurationError, match="not an approved execution handler"):
        plan()


def test_handler_writing_the_wrong_target_is_rejected(broken_pipeline):

    mapping = next(m for m in broken_pipeline["mappings"] if m["code"] == "DW_DIM_SITE")
    mapping["handler"] = "DIM_PRODUCT"

    with pytest.raises(ConfigurationError, match="may only write dw.DimProduct"):
        plan()


def test_unapproved_handler_is_rejected(broken_pipeline):

    broken_pipeline["handlers"]["DIM_SITE"]["approved"] = False

    with pytest.raises(ConfigurationError, match="not approved"):
        plan()


def test_circular_dependency_is_rejected(broken_pipeline):

    site = next(m for m in broken_pipeline["mappings"] if m["code"] == "DW_DIM_SITE")
    sales = next(m for m in broken_pipeline["mappings"] if m["code"] == "DW_FACT_SALES")
    site["depends_on"].append("DW_FACT_SALES")
    assert "DW_DIM_SITE" in sales["depends_on"]

    with pytest.raises(ConfigurationError, match="Circular"):
        plan()


def test_unjustified_tolerance_is_rejected(broken_pipeline):

    mapping = next(m for m in broken_pipeline["mappings"] if m["code"] == "DW_FACT_SALES")
    control = next(c for c in mapping["controls"] if c["name"] == "TOTAL_AMOUNT")
    control.pop("tolerance_reason")

    with pytest.raises(ConfigurationError, match="tolerance_reason"):
        plan()


def test_missing_lineage_is_rejected(broken_pipeline):

    mapping = next(m for m in broken_pipeline["mappings"] if m["code"] == "DW_FACT_SALES")
    mapping.pop("columns")

    with pytest.raises(ConfigurationError, match="column lineage"):
        plan()


def test_pipeline_yaml_parses_without_duplicate_keys():
    """yaml.safe_load silently keeps the last duplicate key; refuse duplicates."""

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        duplicates = {k for k in keys if keys.count(k) > 1}
        assert not duplicates, f"duplicate keys: {duplicates}"
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)

    for name in ("pipeline.yaml", "source_registry.yaml", "data_dictionary.yaml", "business_rules.yaml"):
        with open(metadata_sync.CONFIG_DIR / name, encoding="utf-8") as file:
            yaml.load(file, Loader=UniqueKeyLoader)  # noqa: S506 - UniqueKeyLoader extends SafeLoader


def test_generated_catalogue_documentation_is_up_to_date():
    """docs/catalogue is generated from config; regenerate after config changes."""

    import importlib.util

    path = metadata_sync.ROOT / "scripts" / "generate_docs.py"
    spec = importlib.util.spec_from_file_location("generate_docs", path)
    generate_docs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate_docs)

    for name, content in generate_docs.render().items():
        committed = (generate_docs.OUTPUT / name).read_text(encoding="utf-8")
        assert committed == content, f"docs/catalogue/{name} is stale: run python scripts/generate_docs.py"
