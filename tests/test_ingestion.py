import csv

from pathlib import Path

import openpyxl
import pytest

import discover_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = PROJECT_ROOT / "tests" / "test_data"

REGISTRY = {
    "supplier_master": {"format": "xlsx", "path": "supplier_master.xlsx"},
    "branch_cashup": {"format": "pdf", "path": "cashups/*.pdf"},
}


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Point the ingestion module at an isolated folder tree."""

    incoming = tmp_path / "incoming"
    incoming.mkdir()

    # Paths are written relative to PROJECT_ROOT, so it must contain them.
    monkeypatch.setattr(discover_files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(discover_files, "INCOMING_DIR", incoming)
    monkeypatch.setattr(discover_files, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(discover_files, "QUARANTINE_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(discover_files, "MANIFEST_FILE", tmp_path / "manifest.csv")

    return incoming


def make_workbook(path: Path) -> Path:

    workbook = openpyxl.Workbook()
    workbook.active.append(["supplier_id"])
    workbook.active.append(["SUP0001"])
    workbook.save(path)

    return path


def read_manifest(path: Path) -> list[dict]:

    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def ingest(file: Path, run_id: str, hashes: dict) -> dict:

    return discover_files.process_file(file, run_id, REGISTRY, [], hashes)


def test_identify_source_matches_whole_relative_path(pipeline):

    assert discover_files.identify_source(
        pipeline / "supplier_master.xlsx", REGISTRY
    ) == "supplier_master"

    assert discover_files.identify_source(
        pipeline / "cashups" / "S001_SHIFT-000001_20260921_Cashup.pdf", REGISTRY
    ) == "branch_cashup"

    # A file in an unexpected sub-folder is not silently accepted.
    assert discover_files.identify_source(
        pipeline / "old" / "supplier_master.xlsx", REGISTRY
    ) is None


def test_valid_file_is_ingested_then_detected_as_duplicate(pipeline, tmp_path):

    file = make_workbook(pipeline / "supplier_master.xlsx")
    hashes = {}

    first = ingest(file, "ING-1", hashes)
    second = ingest(file, "ING-2", hashes)

    assert first["status"] == "INGESTED"
    assert (tmp_path / "raw" / "ING-1" / "supplier_master.xlsx").exists()

    assert second["status"] == "SKIPPED_DUPLICATE"
    assert not (tmp_path / "raw" / "ING-2").exists()

    statuses = [row["status"] for row in read_manifest(tmp_path / "manifest.csv")]
    assert statuses == ["INGESTED", "SKIPPED_DUPLICATE"]


def test_renamed_non_excel_file_is_rejected_and_quarantined(pipeline, tmp_path):

    file = pipeline / "supplier_master.xlsx"
    file.write_bytes((TEST_DATA / "corrupted_supplier_master.xlsx").read_bytes())

    outcome = ingest(file, "ING-1", {})

    assert outcome["status"] == "INVALID_CONTENT"
    assert (tmp_path / "quarantine" / "ING-1" / "supplier_master.xlsx").exists()
    assert not (tmp_path / "raw").exists()


def test_rejection_message_names_the_data_steward(pipeline):

    registry = {
        "supplier_master": {
            **REGISTRY["supplier_master"],
            "governance": {"data_steward": "Supplier Master Data Steward (Procurement)"},
        },
    }

    file = pipeline / "supplier_master.xlsx"
    file.write_bytes(b"not a workbook")

    outcome = discover_files.process_file(file, "ING-1", registry, [], {})

    assert outcome["status"] == "INVALID_CONTENT"
    assert "Notify: Supplier Master Data Steward (Procurement)." in outcome["message"]


def test_empty_file_is_rejected(pipeline):

    file = pipeline / "supplier_master.xlsx"
    file.write_bytes(b"")

    assert ingest(file, "ING-1", {})["status"] == "EMPTY_FILE"


def test_unregistered_file_is_logged(pipeline, tmp_path):

    file = make_workbook(pipeline / "mystery.xlsx")

    assert ingest(file, "ING-1", {})["status"] == "UNREGISTERED"
    assert read_manifest(tmp_path / "manifest.csv")[0]["source"] == "UNKNOWN"


def test_manifest_header_upgrade_keeps_existing_rows(pipeline, tmp_path):

    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "run_id,source,file_name,file_hash,ingested_at,raw_path,status\n"
        "ING-0,supplier_master,supplier_master.xlsx,abc,2026-10-04,raw/x,INGESTED\n",
        encoding="utf-8",
    )

    discover_files.upgrade_manifest_header()

    rows = read_manifest(manifest)
    assert list(rows[0]) == discover_files.MANIFEST_FIELDS
    assert discover_files.load_ingested_hashes() == {
        "abc": "ING-0/supplier_master.xlsx"
    }
