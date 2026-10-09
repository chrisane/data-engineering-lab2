"""
Trace a file through the pipeline and explain exactly why it passed
or failed.

Shows, for one file:
    1. Ingestion history   every run that saw the file, its status,
                           hash, storage path and the reason
    2. Validation history  every validation of the traced version
    3. Failures            each failed check with Excel row, record key,
                           field, actual vs expected value, message and
                           recommended action (or the technical error
                           and code location if validation crashed)
    4. Coverage            whether every configured check ran
    5. Load eligibility    the latest gate decision and its reason
    and a one-line verdict.

Usage:
    python scripts/trace_file.py general_ledger.xlsx
    python scripts/trace_file.py general_ledger            # by source name
    python scripts/trace_file.py SHIFT-000082              # part of a name
    python scripts/trace_file.py supplier_master.xlsx --run-id ING-20261004_154838
    python scripts/trace_file.py general_ledger --examples 25
"""

import argparse
import os
import sys

from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(PROJECT_ROOT / "src" / "validation"))

from check_coverage import assess_file, index_results  # noqa: E402
from common import (  # noqa: E402
    DATA_STAGE,
    ELIGIBILITY_LOG_FILE,
    FILE_STATUS_LOG_FILE,
    STRUCTURE_STAGE,
    VALIDATION_LOG_FILE,
    failure_summary,
    latest_file_status,
    load_source_registry,
    read_log,
    read_manifest,
)


# ---------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"

GOOD = {
    "INGESTED", "STRUCTURE_VALID", "DATA_VALID", "COMPLETE",
    "NOT_APPLICABLE", "ELIGIBLE", "PASS",
}
NOTICE = {
    "SKIPPED_DUPLICATE", "IGNORED", "DATA_VALID_WITH_WARNINGS",
    "STOPPED_AT_FAILURE", "HELD", "WARNING",
}


def enable_ansi_colours() -> None:

    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()

        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)

    except Exception:
        pass


def status_text(status: str | None, width: int = 0) -> str:

    status = status or "-"
    colour = GREEN if status in GOOD else YELLOW if status in NOTICE else RED

    return f"{colour}{status:<{width}}{RESET}"


def heading(number: int, title: str) -> None:

    print(f"\n{BOLD}{number}. {title}{RESET}")


# ---------------------------------------------------------
# FILE RESOLUTION
# ---------------------------------------------------------

def resolve_file(query: str, manifest: list[dict], registry: dict) -> list[str]:
    """
    Return the manifest file_name(s) matching the query: an exact file
    name, a single-file source name, or part of a file name.
    """

    names = sorted({row["file_name"] for row in manifest})

    exact = [name for name in names if Path(name).name == query or name == query]

    if exact:
        return exact

    if query in registry:
        by_source = sorted({
            row["file_name"] for row in manifest if row["source"] == query
        })
        if by_source:
            return by_source

    return [name for name in names if query.lower() in name.lower()]


# ---------------------------------------------------------
# SECTIONS
# ---------------------------------------------------------

def show_ingestion(rows: list[dict]) -> None:

    heading(1, "INGESTION HISTORY")

    for row in rows:

        path = row.get("raw_path") or row.get("quarantine_path") or ""
        file_hash = (row.get("file_hash") or "")[:12] or "-"

        print(
            f"  {row['run_id']}  {row['ingested_at']}  "
            f"{status_text(row['status'], 18)} {file_hash:<13} {path}"
        )

        if row.get("message"):
            print(f"  {DIM}{'':<21}{row['message']}{RESET}")


def results_for(
    status_row: dict,
    file_name: str,
    stage: str,
    result_rows: list[dict],
    results_index: dict,
) -> list[dict]:
    """Results produced by the validation behind a status row."""

    if status_row.get("validation_id"):
        return results_index.get((status_row["validation_id"], file_name), [])

    # Validated before validation IDs existed: match by run, file and stage.
    stages = {STRUCTURE_STAGE} if stage == STRUCTURE_STAGE else {
        DATA_STAGE, "BUSINESS_RULE_VALIDATION",
    }

    return [
        row for row in result_rows
        if row["run_id"] == status_row["run_id"]
        and row["file"] == file_name
        and row["stage"] in stages
        and not row.get("validation_id")
    ]


def show_validation_history(status_rows: list[dict]) -> None:

    heading(2, "VALIDATION HISTORY")

    if not status_rows:
        print("  This version has not been validated yet.")
        return

    for row in status_rows:

        print(
            f"  {row.get('validation_id') or '(no validation ID)':<38} "
            f"{row['stage']:<21} {status_text(row['status'], 26)} "
            f"{row['errors'] or 0} error(s), {row['warnings'] or 0} warning(s)  "
            f"{DIM}{row['validated_at']}{RESET}"
        )

        if row.get("quarantine_path"):
            print(f"  {DIM}{'':<38} Quarantined: {row['quarantine_path']}{RESET}")


def show_failures(stage_results: dict[str, list[dict]], examples: int) -> None:

    heading(3, "FAILURES (latest validation of this version)")

    any_failures = False

    for stage, rows in stage_results.items():

        failures = [row for row in rows if row["status"] == "FAIL"]

        if not failures:
            continue

        any_failures = True

        groups = defaultdict(list)

        for row in failures:
            groups[(row["severity"], row["rule"], row["field"])].append(row)

        print(f"  {BOLD}{stage}{RESET}")

        for (severity, rule, field), group in sorted(
            groups.items(), key=lambda item: (item[0][0] != "ERROR", item[0][1]),
        ):

            label = f"{rule}" + (f" on '{field}'" if field else "")
            print(f"  {status_text(severity)} {BOLD}{label}{RESET} - {len(group)} logged failure(s)")

            for row in group[:examples]:

                location = []
                if row.get("sheet"):
                    location.append(f"sheet {row['sheet']}")
                if row.get("row_number"):
                    location.append(f"row {row['row_number']}")
                if row.get("record_key"):
                    location.append(row["record_key"])

                print(f"      {', '.join(location) or 'file level'}")
                print(f"        {row['message']}")

                if row.get("actual") or row.get("expected"):
                    print(
                        f"        {DIM}actual: {row.get('actual') or '(blank)'}"
                        f"  |  expected: {row.get('expected') or '-'}{RESET}"
                    )

                if row.get("technical_error"):
                    print(f"        {DIM}technical: {row.get('error_type')}: {row['technical_error']}{RESET}")

            if len(group) > examples:
                print(f"      {DIM}... {len(group) - examples} more (use --examples to show more){RESET}")

            if group[0].get("recommended_action"):
                print(f"      Action: {group[0]['recommended_action']}")

    if not any_failures:
        print("  No failed checks.")


def show_coverage(assessments: list[dict]) -> None:

    heading(4, "COVERAGE (did every configured check run?)")

    for assessment in assessments:

        print(
            f"  {assessment['stage']:<21} "
            f"{status_text(assessment['coverage_status'], 19)} {assessment['detail']}"
        )

        if assessment.get("missing_checks"):
            print(f"  {DIM}{'':<21} Not executed: {assessment['missing_checks']}{RESET}")


def show_eligibility(decision: dict | None, run_id: str) -> None:

    heading(5, "LOAD ELIGIBILITY")

    if decision is None:
        print("  No load decision recorded for this file yet "
              "(run src/validation/load_eligibility.py).")
        return

    print(
        f"  {status_text(decision['decision'])}  {decision['reason']}\n"
        f"  {DIM}{decision['decision_id']} at {decision['decided_at']}"
        f" (version {decision['run_id']}){RESET}"
    )

    if decision["run_id"] != run_id:
        print(f"  {YELLOW}Note: this decision concerns version {decision['run_id']}, "
              f"not the traced version {run_id}.{RESET}")


# ---------------------------------------------------------
# VERDICT
# ---------------------------------------------------------

def verdict(
    latest_ingestion: dict,
    traced_version: dict | None,
    structure: dict | None,
    data: dict | None,
    stage_results: dict,
    assessments: list[dict],
) -> str:

    if traced_version is None:
        return (
            f"Never ingested. Latest attempt ({latest_ingestion['run_id']}) was "
            f"{latest_ingestion['status']}: {latest_ingestion.get('message') or 'no reason recorded'}"
        )

    if (
        latest_ingestion["run_id"] > traced_version["run_id"]
        and latest_ingestion["status"] not in ("INGESTED", "SKIPPED_DUPLICATE", "IGNORED")
    ):
        return (
            f"A newer submission ({latest_ingestion['run_id']}) was rejected at "
            f"ingestion ({latest_ingestion['status']}): "
            f"{latest_ingestion.get('message')} The traced version "
            f"{traced_version['run_id']} is stale and will not be loaded."
        )

    if structure is None:
        return "Ingested but not yet validated - run the validation scripts."

    if structure["status"] != "STRUCTURE_VALID":
        return (
            f"Failed structural validation ({structure['status']}): "
            f"{failure_summary(stage_results.get(STRUCTURE_STAGE, []))}"
        )

    if data and data["status"] not in ("DATA_VALID", "DATA_VALID_WITH_WARNINGS"):
        return (
            f"Failed data validation ({data['status']}, {data['errors']} error(s)): "
            f"{failure_summary(stage_results.get(DATA_STAGE, []))}"
        )

    gaps = [a for a in assessments if a["coverage_status"] in ("INCOMPLETE", "MISSING", "UNVERIFIABLE")]

    if gaps:
        return f"Validation incomplete ({gaps[0]['stage']}): {gaps[0]['detail']}"

    if data and data["status"] == "DATA_VALID_WITH_WARNINGS":
        return f"Passed with {data['warnings']} warning(s); all checks ran."

    return "Passed validation; all configured checks ran."


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main() -> int:

    parser = argparse.ArgumentParser(description="Explain why a file passed or failed.")
    parser.add_argument("query", help="File name, source name, or part of a file name.")
    parser.add_argument("--run-id", help="Trace the version ingested in this run.")
    parser.add_argument(
        "--examples", type=int, default=5,
        help="Failures shown per rule (default 5). All are in logs/validation_results.csv.",
    )
    args = parser.parse_args()

    enable_ansi_colours()

    manifest = read_manifest()
    registry = load_source_registry()

    matches = resolve_file(args.query, manifest, registry)

    if not matches:
        print(f"No file matching '{args.query}' appears in the ingestion manifest.")
        print("Check data/incoming/ and run ingestion; files never discovered leave no record.")
        return 1

    if len(matches) > 1:
        print(f"'{args.query}' matches {len(matches)} files - be more specific:")
        for name in matches[:30]:
            print(f"  {name}")
        if len(matches) > 30:
            print(f"  ... {len(matches) - 30} more")
        return 1

    file_name = matches[0]
    short_name = Path(file_name).name

    ingestion_rows = [row for row in manifest if row["file_name"] == file_name]
    ingested = [row for row in ingestion_rows if row["status"] == "INGESTED"]

    if args.run_id:
        traced = next((row for row in ingested if row["run_id"] == args.run_id), None)
        if traced is None:
            print(f"{file_name} was not ingested in run {args.run_id}.")
            return 1
    else:
        traced = max(ingested, key=lambda row: row["run_id"]) if ingested else None

    source = (traced or ingestion_rows[-1])["source"]
    source_config = registry.get(source, {})
    governance = source_config.get("governance") or {}

    print(f"\n{BOLD}FILE TRACE: {file_name}{RESET}")
    print(
        f"Source: {source}  |  Owner: {governance.get('data_owner', '-')}  |  "
        f"Steward: {governance.get('data_steward', '-')}  |  "
        f"Classification: {governance.get('classification', '-')}"
    )
    if traced:
        print(f"Traced version: {traced['run_id']}  (hash {traced['file_hash'][:12]}, {traced['raw_path']})")

    show_ingestion(ingestion_rows)

    status_rows = read_log(FILE_STATUS_LOG_FILE)
    result_rows = read_log(VALIDATION_LOG_FILE)
    results_index = index_results(result_rows)

    structure = data = None
    stage_results = {}
    assessments = []

    if traced:

        run_id = traced["run_id"]

        version_status_rows = [
            row for row in status_rows
            if row["run_id"] == run_id and row["file"] == short_name
        ]

        show_validation_history(version_status_rows)

        structure = latest_file_status(status_rows, run_id, short_name, STRUCTURE_STAGE)
        data = latest_file_status(status_rows, run_id, short_name, DATA_STAGE)

        for stage, status_row in ((STRUCTURE_STAGE, structure), (DATA_STAGE, data)):
            if status_row:
                stage_results[stage] = results_for(
                    status_row, short_name, stage, result_rows, results_index,
                )

        show_failures(stage_results, args.examples)

        if source_config:
            assessments = assess_file(
                source, short_name, run_id, source_config, status_rows, results_index,
            )
            show_coverage(assessments)

        decisions = [
            row for row in read_log(ELIGIBILITY_LOG_FILE)
            if row.get("file") == short_name and row["source"] == source
        ]
        show_eligibility(decisions[-1] if decisions else None, run_id)

    print(f"\n{BOLD}VERDICT:{RESET} " + verdict(
        ingestion_rows[-1], traced, structure, data, stage_results, assessments,
    ))
    print()

    return 0


if __name__ == "__main__":

    sys.exit(main())
