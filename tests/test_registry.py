"""
Checks on config/source_registry.yaml itself, so an incomplete or
inconsistent contract is caught before the pipeline runs.
"""

import pytest

from common import load_registry_config


REQUIRED_GOVERNANCE_FIELDS = [
    "description",
    "data_owner",
    "data_steward",
    "system_of_record",
    "classification",
    "contains_personal_data",
    "retention_years",
    "update_frequency",
]

CONFIG = load_registry_config()
POLICY = CONFIG["governance_policy"]
SOURCES = CONFIG["sources"]


@pytest.mark.parametrize("source", SOURCES)
def test_every_source_has_complete_governance(source):

    governance = SOURCES[source].get("governance") or {}

    missing = [
        field
        for field in REQUIRED_GOVERNANCE_FIELDS
        if governance.get(field) in (None, "")
    ]

    assert not missing, f"{source} is missing governance fields: {missing}"


@pytest.mark.parametrize("source", SOURCES)
def test_classification_is_a_defined_level(source):

    classification = SOURCES[source]["governance"]["classification"]

    assert classification in POLICY["classification_levels"]


@pytest.mark.parametrize("source", SOURCES)
def test_personal_data_is_declared_consistently(source):

    config = SOURCES[source]
    governance = config["governance"]
    fields = governance.get("personal_data_fields") or []

    if not governance["contains_personal_data"]:
        assert not fields, f"{source} lists personal data fields but says it has none"
        return

    assert fields, f"{source} contains personal data but lists no fields"

    assert governance["classification"] in POLICY["personal_data_classifications"], (
        f"{source} holds personal data, so it must be classified as one of "
        f"{POLICY['personal_data_classifications']}"
    )

    # Every declared field must actually exist in the source contract.
    structure = config.get("structure") or {}
    known_fields = (
        structure.get("required_columns")
        or structure.get("required_labels")
        or []
    )

    unknown = [field for field in fields if field not in known_fields]

    assert not unknown, f"{source} declares unknown personal data fields: {unknown}"


@pytest.mark.parametrize("source", SOURCES)
def test_retention_is_a_positive_number_of_years(source):

    retention = SOURCES[source]["governance"]["retention_years"]

    assert isinstance(retention, int) and retention > 0
