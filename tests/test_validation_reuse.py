"""
Validation reuse: an unchanged file is not re-read.

A held file version is validated again only when it is new, when its
source contract or the validation code changed since it was validated,
when its last validation crashed, or when --force is given. Otherwise
the recorded result stands (coverage and the load gate read it as before).
"""

import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

import common
from common import STRUCTURE_STAGE, ValidationTarget, contract_checksum, held_targets, plan_validation

CASHUPS = {"format": "pdf", "path": "cashups/*.pdf", "structure": {"required_labels": ["Shift ID"]}}
LEDGER = {"format": "xlsx", "path": "general_ledger.xlsx", "structure": {"sheet": "Sheet1"}}

REGISTRY = {
    "sources": {"general_ledger": LEDGER, "branch_cashup": CASHUPS},
    "withdrawn_files": [{"pattern": "cashups/S*_Cashup.pdf"}],
}


def manifest_row(run_id, source, file_name):

    return {
        "run_id": run_id,
        "source": source,
        "file_name": file_name,
        "status": "INGESTED",
        "raw_path": f"data/raw/{run_id}/{file_name}",
    }


MANIFEST = [
    manifest_row("ING-1", "general_ledger", "general_ledger.xlsx"),
    manifest_row("ING-2", "general_ledger", "general_ledger.xlsx"),
    manifest_row("ING-1", "branch_cashup", "cashups/BR001_A.pdf"),  # still held (not resubmitted)
    manifest_row("ING-1", "branch_cashup", "cashups/BR002_B.pdf"),
    manifest_row("ING-2", "branch_cashup", "cashups/BR002_B.pdf"),  # newer version replaces ING-1
    manifest_row("ING-1", "branch_cashup", "cashups/S001_LEGACY_Cashup.pdf"),  # withdrawn
]


def status(run_id, file_name, checksum, state="STRUCTURE_VALID"):

    return {
        "run_id": run_id,
        "file": Path(file_name).name,
        "stage": STRUCTURE_STAGE,
        "status": state,
        "contract_checksum": checksum,
    }


@pytest.fixture
def logs(monkeypatch):
    """Point the planner at an in-memory manifest and status log."""

    rows = []
    monkeypatch.setattr(common, "read_manifest", lambda: MANIFEST)
    monkeypatch.setattr(common, "read_log", lambda path: rows)
    return rows


def args(**overrides):

    return Namespace(**{"file": None, "run_id": None, "source": None, "force": False, **overrides})


def planned(to_validate):

    return sorted((t.run_id, t.source, t.path.name) for t in to_validate)


def checksum(source):

    return contract_checksum(REGISTRY["sources"][source], {})


# ---------------------------------------------------------
# Which versions are held
# ---------------------------------------------------------


def test_held_versions_match_what_the_gate_decides_on():

    targets = held_targets(REGISTRY, MANIFEST)

    assert sorted((t.run_id, t.source, t.path.name) for t in targets) == [
        ("ING-1", "branch_cashup", "BR001_A.pdf"),  # older run, still the current version
        ("ING-2", "branch_cashup", "BR002_B.pdf"),
        ("ING-2", "general_ledger", "general_ledger.xlsx"),
    ]


# ---------------------------------------------------------
# Reuse decisions
# ---------------------------------------------------------


def test_nothing_is_reread_when_nothing_is_new_or_changed(logs):

    logs += [
        status("ING-2", "general_ledger.xlsx", checksum("general_ledger")),
        status("ING-1", "BR001_A.pdf", checksum("branch_cashup")),
        status("ING-2", "BR002_B.pdf", checksum("branch_cashup")),
    ]

    to_validate, reused, _ = plan_validation(args(), STRUCTURE_STAGE, REGISTRY)

    assert to_validate == []
    assert len(reused) == 3


def test_a_new_version_is_validated_and_the_rest_reused(logs):

    logs += [status("ING-1", "BR001_A.pdf", checksum("branch_cashup"))]

    to_validate, reused, _ = plan_validation(args(), STRUCTURE_STAGE, REGISTRY)

    assert planned(to_validate) == [
        ("ING-2", "branch_cashup", "BR002_B.pdf"),
        ("ING-2", "general_ledger", "general_ledger.xlsx"),
    ]
    assert len(reused) == 1


def test_a_contract_change_revalidates_every_held_version_of_that_source_only(logs):
    """Including versions held from older runs (the cash-up template case)."""

    old_cashup_contract = contract_checksum({**CASHUPS, "structure": {"required_labels": []}}, {})
    logs += [
        status("ING-2", "general_ledger.xlsx", checksum("general_ledger")),
        status("ING-1", "BR001_A.pdf", old_cashup_contract),
        status("ING-2", "BR002_B.pdf", old_cashup_contract),
    ]

    to_validate, reused, _ = plan_validation(args(), STRUCTURE_STAGE, REGISTRY)

    assert planned(to_validate) == [
        ("ING-1", "branch_cashup", "BR001_A.pdf"),
        ("ING-2", "branch_cashup", "BR002_B.pdf"),
    ]
    assert [row["file"] for row in reused] == ["general_ledger.xlsx"]


def test_validations_recorded_before_checksums_existed_are_redone_once(logs):

    logs += [status("ING-2", "general_ledger.xlsx", "")]

    to_validate, _, _ = plan_validation(args(source="general_ledger"), STRUCTURE_STAGE, REGISTRY)

    assert planned(to_validate) == [("ING-2", "general_ledger", "general_ledger.xlsx")]


def test_a_crashed_validation_is_never_reused(logs):

    logs += [status("ING-2", "general_ledger.xlsx", checksum("general_ledger"), state="VALIDATION_ERROR")]

    to_validate, _, _ = plan_validation(args(source="general_ledger"), STRUCTURE_STAGE, REGISTRY)

    assert len(to_validate) == 1


def test_an_invalid_result_is_reused_like_a_valid_one(logs):
    """A file that failed stays failed until it is resubmitted or the rules change."""

    logs += [status("ING-2", "general_ledger.xlsx", checksum("general_ledger"), state="STRUCTURE_INVALID")]

    to_validate, reused, _ = plan_validation(args(source="general_ledger"), STRUCTURE_STAGE, REGISTRY)

    assert to_validate == [] and reused[0]["status"] == "STRUCTURE_INVALID"


def test_force_validates_everything(logs):

    logs += [status("ING-2", "general_ledger.xlsx", checksum("general_ledger"))]

    to_validate, reused, _ = plan_validation(args(force=True), STRUCTURE_STAGE, REGISTRY)

    assert len(to_validate) == 3 and reused == []


# ---------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------


def test_contract_checksum_is_stable_and_sensitive_to_rules_and_settings():

    assert checksum("general_ledger") == checksum("general_ledger")
    assert contract_checksum({**LEDGER, "data_rules": {"unique": ["journal_id"]}}, {}) != checksum("general_ledger")
    assert contract_checksum(LEDGER, {"max_failures_per_rule": 10}) != checksum("general_ledger")


def test_contract_checksum_covers_the_validation_code():

    names = {path.name for path in common.VALIDATION_CODE}

    assert {"validate_structure.py", "validate_data.py", "common.py", "document_extraction.py"} <= names
    assert all(path.exists() for path in common.VALIDATION_CODE)


# ---------------------------------------------------------
# Status board
# ---------------------------------------------------------


def load_launcher():

    path = Path(__file__).resolve().parents[1] / "scripts" / "run_pipeline.py"
    spec = importlib.util.spec_from_file_location("run_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_board_reports_reuse_as_success_not_a_crash():

    launcher = load_launcher()
    output = "VALIDATION SUMMARY\nFiles validated:   0\nUnchanged (reused): 100\n"

    status_, detail = launcher.interpret_validation(0, [], {"STRUCTURE_VALID"}, set(), launcher.reused_count(output))

    assert status_ == "SUCCESS"
    assert "100 held file(s) already validated" in detail


def test_status_board_still_reports_a_real_crash():

    launcher = load_launcher()

    assert launcher.interpret_validation(2, [], {"STRUCTURE_VALID"}, set(), 0)[0] == "ERROR"
    assert launcher.interpret_validation(0, [], {"STRUCTURE_VALID"}, set(), 0)[0] == "ERROR"


def test_validation_target_keeps_its_own_run():

    target = ValidationTarget("ING-1", "branch_cashup", Path("x.pdf"))

    assert target.run_id == "ING-1"
