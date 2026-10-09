"""
Load eligibility gate.

Decides, for every registered source, which file version may be loaded
into SQL and records why. The SQL loader must load only files marked
ELIGIBLE in the latest decision.

Selection
    - Single-file sources: the most recently ingested version. If that
      version failed, the source is BLOCKED - an older valid version is
      not loaded in its place, because it would be stale.
    - Document sets (path with a wildcard, e.g. cash-up PDFs): the latest
      version of each document.

Decision per file
    ELIGIBLE  passed validation and every configured check ran
    BLOCKED   not validated, failed validation, or validation incomplete
    HELD      valid, but a critical source is blocked, so nothing loads

Exit codes: 0 = all eligible, 1 = some files blocked (the rest can
load), 2 = load held because a critical source is blocked.

Usage:
    python src/validation/load_eligibility.py
"""

import sys

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from check_coverage import PASSING_COVERAGE, assess_file, index_results
from common import (
    DATA_STAGE,
    ELIGIBILITY_LOG_FILE,
    FILE_STATUS_LOG_FILE,
    STRUCTURE_STAGE,
    VALIDATION_LOG_FILE,
    append_csv_rows,
    failure_summary,
    latest_file_status,
    load_registry_config,
    read_log,
    read_manifest,
)
from validate_data import has_row_rules


ELIGIBILITY_FIELDS = [
    "decision_id",
    "decided_at",
    "source",
    "file",
    "raw_path",
    "run_id",
    "file_hash",
    "ingested_at",
    "classification",
    "critical",
    "structure_status",
    "structure_validation_id",
    "data_status",
    "data_validation_id",
    "errors",
    "warnings",
    "decision",
    "reason",
    "data_steward",
]


# ---------------------------------------------------------
# VERSION SELECTION
# ---------------------------------------------------------

def is_document_set(source_config: dict) -> bool:

    return any(character in source_config["path"] for character in "*?[")


# Ingestion outcomes that mean a submission was refused.
REJECTED_INGESTION_STATUSES = {
    "INVALID_FORMAT",
    "INVALID_CONTENT",
    "EMPTY_FILE",
    "INGESTION_ERROR",
}


def latest_rejection_after(
    manifest: list[dict],
    source: str,
    file_name: str | None,
    after_run_id: str,
) -> dict | None:
    """
    A submission of this file (or, for file_name None, of this source)
    that was rejected at ingestion after the given run. Its existence
    means the latest version we hold is stale.
    """

    rejections = [
        row for row in manifest
        if row["source"] == source
        and row["status"] in REJECTED_INGESTION_STATUSES
        and row["run_id"] > after_run_id
        and (file_name is None or row["file_name"] == file_name)
    ]

    return max(rejections, key=lambda row: row["run_id"]) if rejections else None


def candidate_versions(
    source: str,
    source_config: dict,
    manifest: list[dict],
) -> list[dict]:

    ingested = [
        row for row in manifest
        if row["source"] == source and row["status"] == "INGESTED"
    ]

    if not ingested:
        return []

    if not is_document_set(source_config):
        return [max(ingested, key=lambda row: row["run_id"])]

    latest = {}

    for row in ingested:

        name = row["file_name"]

        if name not in latest or row["run_id"] > latest[name]["run_id"]:
            latest[name] = row

    return sorted(latest.values(), key=lambda row: row["file_name"])


# ---------------------------------------------------------
# DECISIONS
# ---------------------------------------------------------

def decide_version(
    manifest_row: dict,
    source: str,
    source_config: dict,
    policy: dict,
    manifest: list[dict],
    status_rows: list[dict],
    results_index: dict,
) -> dict:

    file_name = Path(manifest_row["file_name"]).name
    run_id = manifest_row["run_id"]
    governance = source_config.get("governance") or {}

    structure = latest_file_status(status_rows, run_id, file_name, STRUCTURE_STAGE)
    data = latest_file_status(status_rows, run_id, file_name, DATA_STAGE)
    applies_data_rules = has_row_rules(source_config)

    decision = {
        "source": source,
        "file": file_name,
        "raw_path": manifest_row["raw_path"],
        "run_id": run_id,
        "file_hash": manifest_row["file_hash"],
        "ingested_at": manifest_row["ingested_at"],
        "classification": governance.get("classification"),
        "data_steward": governance.get("data_steward"),
        "structure_status": structure["status"] if structure else None,
        "structure_validation_id": structure.get("validation_id") if structure else None,
        "data_status": data["status"] if data else None,
        "data_validation_id": data.get("validation_id") if data else None,
    }

    latest = data if applies_data_rules and data else structure

    if latest:
        decision["errors"] = latest.get("errors")
        decision["warnings"] = latest.get("warnings")

    def blocked(reason: str) -> dict:
        return {**decision, "decision": "BLOCKED", "reason": reason}

    def why(status_row: dict) -> str:
        rows = results_index.get((status_row.get("validation_id"), file_name), [])
        return failure_summary(rows) or "see logs/validation_results.csv"

    # 0. A newer submission was rejected at ingestion, so this version
    #    is stale and must not be loaded in its place.
    rejection = latest_rejection_after(
        manifest, source,
        None if not is_document_set(source_config) else manifest_row["file_name"],
        run_id,
    )

    if rejection:
        return blocked(
            f"A newer submission (run {rejection['run_id']}) was rejected at "
            f"ingestion ({rejection['status']}): {rejection.get('message')} "
            f"The older version from run {run_id} is not loaded because it "
            f"would be stale."
        )

    # 1. Structure
    if structure is None:
        return blocked(f"Not validated: no structural validation result for run {run_id}.")

    if structure["status"] not in policy["eligible_structure_statuses"]:
        return blocked(
            f"Failed structural validation ({structure['status']}): {why(structure)}."
        )

    # 2. Data rules
    if applies_data_rules:

        if data is None:
            return blocked(f"Not validated: no data validation result for run {run_id}.")

        if data["status"] not in policy["eligible_data_statuses"]:
            return blocked(
                f"Failed data validation ({data['status']}, {data['errors']} "
                f"error(s)): {why(data)}."
            )

    # 3. Coverage: a pass only counts if every configured check ran.
    for assessment in assess_file(
        source, file_name, run_id, source_config, status_rows, results_index,
    ):
        if assessment["coverage_status"] not in PASSING_COVERAGE:
            return blocked(
                f"Validation incomplete ({assessment['stage']} "
                f"{assessment['coverage_status']}): {assessment['detail']}"
            )

    if not applies_data_rules:
        reason = "Passed structural validation; no data rules configured (structure checks only)."
    else:
        reason = "Passed structural and data validation; all configured checks ran."

        if data.get("warnings") not in (None, "", "0"):
            reason += f" {data['warnings']} warning(s) - review before relying on the data."

    return {**decision, "decision": "ELIGIBLE", "reason": reason}


def decide(
    registry_config: dict,
    manifest: list[dict],
    status_rows: list[dict],
    result_rows: list[dict],
) -> list[dict]:

    sources = registry_config["sources"]
    policy = registry_config["load_policy"]
    critical_sources = set(policy.get("critical_sources") or [])

    results_index = index_results(result_rows)

    decisions = []

    for source, source_config in sources.items():

        versions = candidate_versions(source, source_config, manifest)

        if not versions:

            governance = source_config.get("governance") or {}

            attempts = [row for row in manifest if row["source"] == source]

            if attempts:
                last = attempts[-1]
                reason = (
                    f"No file has been ingested. Latest submission "
                    f"({last['file_name']}, run {last['run_id']}) was "
                    f"{last['status']}: {last.get('message') or 'no reason recorded'}"
                )
            else:
                reason = (
                    "No file has been ingested and no submission was "
                    "received for this source."
                )

            decisions.append({
                "source": source,
                "file": Path(attempts[-1]["file_name"]).name if attempts else None,
                "run_id": attempts[-1]["run_id"] if attempts else None,
                "classification": governance.get("classification"),
                "data_steward": governance.get("data_steward"),
                "decision": "BLOCKED",
                "reason": reason,
            })
            continue

        for row in versions:
            decisions.append(decide_version(
                row, source, source_config, policy, manifest,
                status_rows, results_index,
            ))

    for decision in decisions:
        decision["critical"] = decision["source"] in critical_sources

    blocked_critical = sorted({
        decision["source"]
        for decision in decisions
        if decision["critical"] and decision["decision"] == "BLOCKED"
    })

    if blocked_critical:

        for decision in decisions:

            if decision["decision"] == "ELIGIBLE":
                decision["decision"] = "HELD"
                decision["reason"] = (
                    f"Valid, but held because critical source(s) "
                    f"{', '.join(blocked_critical)} are blocked; nothing "
                    f"is loaded until they pass."
                )

    return decisions


# ---------------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------------

def main() -> int:

    decisions = decide(
        load_registry_config(),
        read_manifest(),
        read_log(FILE_STATUS_LOG_FILE),
        read_log(VALIDATION_LOG_FILE),
    )

    decision_id = f"LOAD-{datetime.now():%Y%m%d_%H%M%S_%f}"
    decided_at = datetime.now().isoformat(timespec="seconds")

    for decision in decisions:
        decision["decision_id"] = decision_id
        decision["decided_at"] = decided_at

    append_csv_rows(ELIGIBILITY_LOG_FILE, ELIGIBILITY_FIELDS, decisions)

    counts = Counter(decision["decision"] for decision in decisions)

    by_source = defaultdict(list)

    for decision in decisions:
        by_source[decision["source"]].append(decision)

    print(f"\nLOAD ELIGIBILITY")
    print(f"Decision ID: {decision_id}\n")

    for source, source_decisions in by_source.items():

        source_counts = Counter(d["decision"] for d in source_decisions)
        critical = " [critical]" if source_decisions[0]["critical"] else ""

        if len(source_decisions) == 1:
            only = source_decisions[0]
            print(f"{source}{critical} -> {only['decision']} ({only.get('file') or '-'})")
        else:
            summary = ", ".join(f"{count} {name.lower()}" for name, count in source_counts.items())
            print(f"{source}{critical} -> {len(source_decisions)} documents: {summary}")

        for decision in source_decisions:
            if decision["decision"] == "BLOCKED":
                print(f"   BLOCKED {decision.get('file') or ''}: {decision['reason']}")

    print("\nELIGIBILITY SUMMARY")
    print("--------------------------------------------")
    print(f"Eligible: {counts['ELIGIBLE']}")
    print(f"Blocked:  {counts['BLOCKED']}")
    print(f"Held:     {counts['HELD']}")
    print(f"Log:      logs/load_eligibility.csv ({decision_id})")

    if counts["HELD"] or (counts["BLOCKED"] and not counts["ELIGIBLE"]):
        print(
            "Load:     HELD - nothing will be loaded (a critical source is "
            "blocked, or no file is eligible)."
        )
        print("Trace:    python scripts/trace_file.py <file name>")
        return 2

    if counts["BLOCKED"]:
        print("Load:     PARTIAL - only eligible files will be loaded.")
        print("Trace:    python scripts/trace_file.py <file name>")
        return 1

    print("Load:     READY - all files are eligible.")
    return 0


if __name__ == "__main__":

    sys.exit(main())
