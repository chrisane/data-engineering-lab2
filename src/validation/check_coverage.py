"""
Validation coverage check.

A successful exit code must never hide skipped or incomplete
validation. For every file ingested in a run, this proves that:

    - each applicable validation stage produced a result, and
    - every check configured for the source actually executed
      (a PASS or FAIL row exists for it in that validation).

A file whose validation stopped at a failure (for example an
unreadable workbook) is complete as long as the failure is recorded;
later checks legitimately could not run.

Coverage statuses
    COMPLETE            every expected check ran
    STOPPED_AT_FAILURE  validation stopped at a recorded failure
    NOT_APPLICABLE      no rules configured for this stage
    INCOMPLETE          some expected checks did not run
    MISSING             the file was never validated in this stage
    UNVERIFIABLE        validated before validation IDs were recorded

Usage:
    python src/validation/check_coverage.py
    python src/validation/check_coverage.py --run-id ING-20261009_161403
"""

import argparse
import sys

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from common import (
    COVERAGE_LOG_FILE,
    DATA_STAGE,
    FILE_STATUS_LOG_FILE,
    STOPPED_STATUSES,
    STRUCTURE_STAGE,
    VALIDATION_LOG_FILE,
    append_csv_rows,
    ingested_files_for_run,
    latest_file_status,
    latest_ingestion_run,
    load_source_registry,
    read_log,
    read_manifest,
)
from validate_data import expected_data_checks, has_row_rules
from validate_structure import expected_structure_checks


COVERAGE_FIELDS = [
    "coverage_id",
    "checked_at",
    "run_id",
    "source",
    "file",
    "stage",
    "validation_id",
    "validation_status",
    "coverage_status",
    "expected_checks",
    "executed_checks",
    "missing_checks",
    "detail",
]

PASSING_COVERAGE = {"COMPLETE", "STOPPED_AT_FAILURE", "NOT_APPLICABLE"}


# ---------------------------------------------------------
# RESULT INDEX
# ---------------------------------------------------------

def index_results(result_rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group validation results by (validation_id, file name)."""

    index = defaultdict(list)

    for row in result_rows:

        if row.get("validation_id"):
            index[(row["validation_id"], row["file"])].append(row)

    return index


def format_checks(checks: set[tuple[str, str]], limit: int = 10) -> str:

    shown = [
        f"{rule}({field})" if field else rule
        for rule, field in sorted(checks)[:limit]
    ]

    if len(checks) > limit:
        shown.append(f"+{len(checks) - limit} more")

    return "; ".join(shown)


# ---------------------------------------------------------
# ASSESSMENT
# ---------------------------------------------------------

def assess_stage(
    source: str,
    file_name: str,
    run_id: str,
    stage: str,
    expected: set[tuple[str, str]],
    status_rows: list[dict],
    results_index: dict,
) -> dict:

    record = {
        "run_id": run_id,
        "source": source,
        "file": file_name,
        "stage": stage,
        "expected_checks": len(expected),
    }

    status = latest_file_status(status_rows, run_id, file_name, stage)

    if status is None:
        return {
            **record,
            "coverage_status": "MISSING",
            "executed_checks": 0,
            "missing_checks": format_checks(expected),
            "detail": (
                f"No {stage} result exists for this file in run {run_id}. "
                f"Run the validation script for this run."
            ),
        }

    record["validation_id"] = status.get("validation_id")
    record["validation_status"] = status["status"]

    if not status.get("validation_id"):
        return {
            **record,
            "coverage_status": "UNVERIFIABLE",
            "executed_checks": None,
            "detail": (
                "This file was validated before validation IDs were "
                "recorded, so its checks cannot be verified. Re-run "
                "validation for this run."
            ),
        }

    rows = results_index.get((status["validation_id"], file_name), [])

    executed = {(row["rule"], row["field"] or "") for row in rows}
    record["executed_checks"] = len(executed & expected)

    if status["status"] in STOPPED_STATUSES:

        failures = [row for row in rows if row["status"] == "FAIL"]

        if failures:
            return {
                **record,
                "coverage_status": "STOPPED_AT_FAILURE",
                "detail": (
                    f"Validation stopped at {failures[0]['rule']}; later "
                    f"checks could not run. See the failure for the cause."
                ),
            }

        return {
            **record,
            "coverage_status": "INCOMPLETE",
            "detail": (
                f"Status is {status['status']} but no failing check was "
                f"recorded, so the reason cannot be traced."
            ),
        }

    missing = expected - executed

    if missing:
        return {
            **record,
            "coverage_status": "INCOMPLETE",
            "missing_checks": format_checks(missing),
            "detail": (
                f"{len(missing)} of {len(expected)} configured checks did "
                f"not run. A rule may refer to a column the file lacks, or "
                f"validation ended early."
            ),
        }

    return {
        **record,
        "coverage_status": "COMPLETE",
        "detail": f"All {len(expected)} configured checks ran.",
    }


def assess_file(
    source: str,
    file_name: str,
    run_id: str,
    source_config: dict,
    status_rows: list[dict],
    results_index: dict,
) -> list[dict]:
    """Coverage of every validation stage for one ingested file."""

    assessments = [
        assess_stage(
            source, file_name, run_id, STRUCTURE_STAGE,
            expected_structure_checks(source_config),
            status_rows, results_index,
        )
    ]

    if has_row_rules(source_config):

        assessments.append(assess_stage(
            source, file_name, run_id, DATA_STAGE,
            expected_data_checks(source_config),
            status_rows, results_index,
        ))

    else:

        assessments.append({
            "run_id": run_id,
            "source": source,
            "file": file_name,
            "stage": DATA_STAGE,
            "coverage_status": "NOT_APPLICABLE",
            "expected_checks": 0,
            "executed_checks": 0,
            "detail": "No data rules configured; structure checks only.",
        })

    return assessments


def assess_run(
    run_id: str,
    manifest: list[dict],
    source_registry: dict,
    status_rows: list[dict],
    result_rows: list[dict],
) -> list[dict]:

    results_index = index_results(result_rows)

    assessments = []

    for row in ingested_files_for_run(manifest, run_id):

        source = row["source"]

        if source not in source_registry:
            continue

        assessments.extend(assess_file(
            source=source,
            file_name=Path(row["file_name"]).name,
            run_id=run_id,
            source_config=source_registry[source],
            status_rows=status_rows,
            results_index=results_index,
        ))

    return assessments


# ---------------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------------

def main() -> int:

    parser = argparse.ArgumentParser(
        description="Verify that every configured validation check ran."
    )
    parser.add_argument(
        "--run-id",
        help="Ingestion run to check (default: latest run that ingested files).",
    )
    args = parser.parse_args()

    manifest = read_manifest()
    run_id = args.run_id or latest_ingestion_run(manifest)

    if run_id is None:
        raise SystemExit("No ingested files found.")

    coverage_id = f"COV-{datetime.now():%Y%m%d_%H%M%S_%f}"
    checked_at = datetime.now().isoformat(timespec="seconds")

    assessments = assess_run(
        run_id,
        manifest,
        load_source_registry(),
        read_log(FILE_STATUS_LOG_FILE),
        read_log(VALIDATION_LOG_FILE),
    )

    for assessment in assessments:
        assessment["coverage_id"] = coverage_id
        assessment["checked_at"] = checked_at

    append_csv_rows(COVERAGE_LOG_FILE, COVERAGE_FIELDS, assessments)

    files = {assessment["file"] for assessment in assessments}
    counts = Counter(assessment["coverage_status"] for assessment in assessments)

    structure_only = sum(
        1 for assessment in assessments
        if assessment["stage"] == DATA_STAGE
        and assessment["coverage_status"] == "NOT_APPLICABLE"
    )

    print(f"\nVALIDATION COVERAGE: {run_id}")
    print(f"Coverage ID: {coverage_id}\n")

    gaps = [
        assessment for assessment in assessments
        if assessment["coverage_status"] not in PASSING_COVERAGE
    ]

    for gap in gaps:
        print(
            f"{gap['file']} -> {gap['stage']} -> {gap['coverage_status']}\n"
            f"   {gap['detail']}"
        )
        if gap.get("missing_checks"):
            print(f"   Not executed: {gap['missing_checks']}")

    checks_verified = sum(
        assessment.get("executed_checks") or 0
        for assessment in assessments
    )

    print("\nCOVERAGE SUMMARY")
    print("--------------------------------------------")
    print(f"Files:                {len(files)}")
    print(f"Checks verified:      {checks_verified}")
    print(f"Complete:             {counts['COMPLETE']}")
    print(f"Stopped at failure:   {counts['STOPPED_AT_FAILURE']}")
    print(f"Structure checks only: {structure_only}")
    print(f"Incomplete:           {counts['INCOMPLETE']}")
    print(f"Missing:              {counts['MISSING']}")
    print(f"Unverifiable:         {counts['UNVERIFIABLE']}")

    return 1 if gaps else 0


if __name__ == "__main__":

    sys.exit(main())
