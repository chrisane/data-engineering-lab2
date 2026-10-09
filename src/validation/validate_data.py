"""
Row-level data-quality and business-rule validation.

Runs the `data_rules` and `business_rules` of each source contract
(config/source_registry.yaml) against every row of the ingested
file. Files must pass structural validation first.

Data rules (stage DATA_VALIDATION)
    required_values, unique, numeric, dates, allowed_values,
    patterns, references

Business rules (stage BUSINESS_RULE_VALIDATION)
    calculations, comparisons, group_balance

Each failure is logged with run ID, file, sheet, Excel row number,
record key, field, rule, actual and expected values, severity and
recommended action. Files with ERROR-level failures are copied to
data/quarantine/<run_id>/ together with an exception report.

Usage:
    python src/validation/validate_data.py
    python src/validation/validate_data.py --run-id ING-20261004_154838
    python src/validation/validate_data.py --source purchase_orders
    python src/validation/validate_data.py --source supplier_master \
        --file tests/test_data/supplier_master_missing_value.xlsx
"""

import ast
import csv
import math
import operator
import re
import sys

from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from common import (
    CONFIG_DIR,
    DEFAULT_MAX_FAILURES_PER_RULE,
    PROJECT_ROOT,
    is_blank,
    latest_raw_file_for_source,
    load_settings,
    load_source_registry,
    make_result,
    quarantine_file,
    read_manifest,
    read_table,
    select_targets,
    write_file_status,
    write_validation_results,
)
from validate_structure import (
    parse_arguments,
    validate_source as validate_structure,
)


DATA_STAGE = "DATA_VALIDATION"
BUSINESS_STAGE = "BUSINESS_RULE_VALIDATION"

COMPARISON_OPERATORS = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

FORMULA_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


# ---------------------------------------------------------
# VALUE HELPERS
# ---------------------------------------------------------

def to_number(value) -> float | int | None:
    """
    Return the numeric value, or None if the value is not a number.
    Numeric text (as found in CSV files) is accepted.
    """

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return None if math.isnan(value) else value

    if isinstance(value, str):

        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None

    return None


def to_date(value) -> tuple[date | None, bool]:
    """
    Return (parsed_date, stored_as_text). parsed_date is None when
    the value is not a valid date.
    """

    if isinstance(value, (datetime, date)):
        return value, False

    if isinstance(value, str):

        try:
            return datetime.fromisoformat(value.strip()), True
        except ValueError:
            return None, True

    return None, False


def key_of(value) -> str:
    """
    Normalise a value for key comparisons, so that 1010, 1010.0 and
    "1010" all match.
    """

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def value_allowed(value, allowed: list) -> bool:

    if isinstance(value, bool):
        return any(
            isinstance(option, bool) and option == value
            for option in allowed
        )

    number = to_number(value) if not isinstance(value, str) else None

    if number is not None:
        return any(
            not isinstance(option, bool)
            and isinstance(option, (int, float))
            and math.isclose(number, option, abs_tol=1e-9)
            for option in allowed
        )

    return key_of(value) in {key_of(option) for option in allowed}


def display(value):

    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()

    if isinstance(value, float):
        return round(value, 6)

    return value


# ---------------------------------------------------------
# FORMULAS
# ---------------------------------------------------------

def compile_formula(formula: str) -> tuple[ast.AST, list[str]]:
    """
    Parse a calculation formula such as 'net_amount + vat_amount'.
    Only field names, numbers, + - * / and brackets are allowed,
    so formulas in configuration cannot execute arbitrary code.
    """

    tree = ast.parse(formula, mode="eval")

    allowed_nodes = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.USub,
        ast.UAdd,
        ast.Name,
        ast.Load,
        ast.Constant,
        *FORMULA_OPERATORS,
    )

    names = []

    for node in ast.walk(tree):

        if not isinstance(node, allowed_nodes):
            raise ValueError(
                f"Unsupported element '{type(node).__name__}' "
                f"in formula '{formula}'."
            )

        if isinstance(node, ast.Constant) and to_number(node.value) is None:
            raise ValueError(
                f"Only numeric constants are allowed in formula '{formula}'."
            )

        if isinstance(node, ast.Name):
            names.append(node.id)

    return tree.body, names


def evaluate_formula(node: ast.AST, values: dict) -> float:

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        return values[node.id]

    if isinstance(node, ast.UnaryOp):
        operand = evaluate_formula(node.operand, values)
        return -operand if isinstance(node.op, ast.USub) else operand

    return FORMULA_OPERATORS[type(node.op)](
        evaluate_formula(node.left, values),
        evaluate_formula(node.right, values),
    )


# ---------------------------------------------------------
# TABLES AND REFERENCE DATA
# ---------------------------------------------------------

class Table:

    def __init__(
        self,
        headers: list,
        rows: list[tuple[int, tuple]],
    ) -> None:

        self.headers = headers
        self.rows = rows

        # First occurrence wins; duplicate columns are reported by
        # structural validation.
        self.positions = {}

        for index, header in enumerate(headers):
            if not is_blank(header) and header not in self.positions:
                self.positions[header] = index

    def has(self, field: str) -> bool:

        return field in self.positions

    def value(self, row: tuple, field: str):

        index = self.positions.get(field)

        if index is None or index >= len(row):
            return None

        return row[index]


class ReferenceData:
    """
    Loads and caches other sources and config files used by
    referential-integrity and lookup rules.
    """

    def __init__(
        self,
        manifest: list[dict],
        run_id: str,
        source_registry: dict,
    ) -> None:

        self.manifest = manifest
        self.run_id = run_id
        self.source_registry = source_registry
        self.tables = {}

    def table(self, spec: dict) -> tuple[Table | None, str]:
        """
        Return (table, description). table is None if the reference
        data cannot be found.
        """

        if "config" in spec:

            name = spec["config"]
            description = f"config/{name}"
            cache_key = ("config", name)

            if cache_key not in self.tables:

                path = CONFIG_DIR / name

                if not path.exists():
                    self.tables[cache_key] = None
                else:
                    with open(path, "r", newline="", encoding="utf-8-sig") as file:
                        reader = csv.reader(file)
                        headers = next(reader, [])
                        rows = [(number, tuple(row)) for number, row in enumerate(reader, start=2)]
                    self.tables[cache_key] = Table(headers, rows)

            return self.tables[cache_key], description

        source = spec["source"]
        cache_key = ("source", source)

        if cache_key not in self.tables:

            # Ad-hoc runs are not in the manifest; use the latest RAW copy.
            up_to = self.run_id if self.run_id.startswith("ING-") else None

            path = latest_raw_file_for_source(self.manifest, source, up_to)

            if path is None or not path.exists():
                self.tables[cache_key] = None
            else:
                structure = self.source_registry[source].get("structure") or {}
                self.tables[cache_key] = Table(*read_table(path, structure))

        return self.tables[cache_key], source

    def key_set(self, spec: dict) -> set[str] | None:

        table, _ = self.table(spec)

        if table is None or not table.has(spec["field"]):
            return None

        return {
            key_of(table.value(row, spec["field"]))
            for _, row in table.rows
            if not is_blank(table.value(row, spec["field"]))
        }

    def lookup(self, spec: dict) -> dict | None:

        table, _ = self.table(spec)

        if (
            table is None
            or not table.has(spec["source_key"])
            or not table.has(spec["field"])
        ):
            return None

        return {
            key_of(table.value(row, spec["source_key"])): table.value(row, spec["field"])
            for _, row in table.rows
        }


# ---------------------------------------------------------
# FAILURE COLLECTION
# ---------------------------------------------------------

class ResultCollector:
    """
    Builds result records for one file. Caps the number of failures
    logged per rule/field so a systematically broken column cannot
    flood the log, and records a PASS line for every check that
    found no failures (proof the check ran).
    """

    def __init__(
        self,
        run_id: str,
        source: str,
        file: Path,
        sheet: str | None,
        record_key_fields: list[str],
        max_failures_per_rule: int,
    ) -> None:

        self.run_id = run_id
        self.source = source
        self.file = file
        self.sheet = sheet
        self.record_key_fields = record_key_fields
        self.max_failures = max_failures_per_rule

        self.checks = {}
        self.failure_counts = Counter()
        self.failure_severity = {}
        self.results = []

    def record_key(self, table: Table, row: tuple) -> str | None:

        if not self.record_key_fields:
            return None

        return "|".join(
            f"{field}={display(table.value(row, field))}"
            for field in self.record_key_fields
        )

    def check(
        self,
        stage: str,
        rule: str,
        field: str | None,
        description: str,
    ) -> None:

        self.checks.setdefault((stage, rule, field), description)

    def fail(
        self,
        stage: str,
        rule: str,
        field: str | None,
        severity: str,
        message: str,
        recommended_action: str,
        table: Table | None = None,
        row_number: int | None = None,
        row: tuple | None = None,
        actual=None,
        expected=None,
    ) -> None:

        key = (stage, rule, field)

        self.failure_counts[key] += 1
        self.failure_severity[key] = severity

        if self.failure_counts[key] > self.max_failures:
            return

        self.results.append(make_result(
            run_id=self.run_id,
            source=self.source,
            file=self.file,
            sheet=self.sheet,
            row_number=row_number,
            record_key=(
                self.record_key(table, row)
                if table is not None and row is not None
                else None
            ),
            stage=stage,
            rule=rule,
            field=field,
            status="FAIL",
            severity=severity,
            actual=display(actual),
            expected=expected,
            message=message,
            recommended_action=recommended_action,
        ))

    def finalise(self, rows_checked: int) -> list[dict]:

        results = list(self.results)

        for key, count in self.failure_counts.items():

            if count > self.max_failures:

                stage, rule, field = key

                results.append(make_result(
                    run_id=self.run_id,
                    source=self.source,
                    file=self.file,
                    sheet=self.sheet,
                    stage=stage,
                    rule=rule,
                    field=field,
                    status="FAIL",
                    severity=self.failure_severity[key],
                    actual=f"{count} failures",
                    message=(
                        f"{count} failures in total; only the first "
                        f"{self.max_failures} are listed individually."
                    ),
                    recommended_action=(
                        "The issue affects many rows - check for a "
                        "systematic cause in the source extract."
                    ),
                ))

        for (stage, rule, field), description in self.checks.items():

            if self.failure_counts[(stage, rule, field)] == 0:

                results.append(make_result(
                    run_id=self.run_id,
                    source=self.source,
                    file=self.file,
                    sheet=self.sheet,
                    stage=stage,
                    rule=rule,
                    field=field,
                    status="PASS",
                    actual=f"{rows_checked} rows checked",
                    expected=description,
                    message=f"{rule} passed for all rows.",
                ))

        return results


def missing_column(
    collector: ResultCollector,
    stage: str,
    rule: str,
    field: str,
) -> None:
    """A rule refers to a column the file does not have."""

    collector.fail(
        stage, rule, field, "ERROR",
        message=(
            f"Rule {rule} refers to column '{field}', which is not "
            f"in the file."
        ),
        recommended_action=(
            f"Add '{field}' to the file, or correct the rule in "
            f"config/source_registry.yaml (and add the column to "
            f"required_columns)."
        ),
        expected=field,
        actual="COLUMN_NOT_FOUND",
    )


# ---------------------------------------------------------
# DATA RULES
# ---------------------------------------------------------

def check_required_values(table, rules, collector) -> None:

    for field in rules.get("required_values") or []:

        if not table.has(field):
            missing_column(collector, DATA_STAGE, "REQUIRED_VALUE", field)
            continue

        collector.check(DATA_STAGE, "REQUIRED_VALUE", field, "A non-blank value.")

        for row_number, row in table.rows:

            if is_blank(table.value(row, field)):

                collector.fail(
                    DATA_STAGE, "REQUIRED_VALUE", field, "ERROR",
                    message=f"Required field '{field}' is blank.",
                    recommended_action=(
                        f"Enter a valid value for '{field}' in row "
                        f"{row_number} and resubmit the file."
                    ),
                    table=table, row_number=row_number, row=row,
                    actual=None, expected="A non-blank value.",
                )


def check_unique(table, rules, collector) -> None:

    for key in rules.get("unique") or []:

        fields = [key] if isinstance(key, str) else list(key)
        label = "+".join(fields)

        absent = [field for field in fields if not table.has(field)]

        if absent:
            for field in absent:
                missing_column(collector, DATA_STAGE, "UNIQUE", field)
            continue

        collector.check(DATA_STAGE, "UNIQUE", label, f"Unique {label}.")

        first_seen = {}

        for row_number, row in table.rows:

            values = [table.value(row, field) for field in fields]

            if any(is_blank(value) for value in values):
                continue

            normalised = tuple(key_of(value) for value in values)

            if normalised not in first_seen:
                first_seen[normalised] = row_number
                continue

            shown = ", ".join(
                f"{field}={display(value)}"
                for field, value in zip(fields, values)
            )

            collector.fail(
                DATA_STAGE, "UNIQUE", label, "ERROR",
                message=(
                    f"Duplicate {label} ({shown}); first seen in row "
                    f"{first_seen[normalised]}."
                ),
                recommended_action=(
                    f"Remove the duplicate record or correct its "
                    f"{label}. Compare rows {first_seen[normalised]} "
                    f"and {row_number}."
                ),
                table=table, row_number=row_number, row=row,
                actual=shown, expected=f"A unique {label}.",
            )


def check_numeric(table, rules, collector) -> None:

    for field, limits in (rules.get("numeric") or {}).items():

        if not table.has(field):
            missing_column(collector, DATA_STAGE, "DATA_TYPE", field)
            continue

        limits = limits or {}
        minimum = limits.get("minimum")
        maximum = limits.get("maximum")
        integer = limits.get("integer", False)

        expected_type = "A whole number." if integer else "A number."

        collector.check(DATA_STAGE, "DATA_TYPE", field, expected_type)

        range_text = None

        if minimum is not None or maximum is not None:

            range_text = " and ".join(
                part for part in (
                    f">= {minimum}" if minimum is not None else None,
                    f"<= {maximum}" if maximum is not None else None,
                ) if part
            )

            collector.check(DATA_STAGE, "NUMERIC_RANGE", field, range_text)

        for row_number, row in table.rows:

            value = table.value(row, field)

            if is_blank(value):
                continue

            number = to_number(value)

            if number is None or (integer and not float(number).is_integer()):

                collector.fail(
                    DATA_STAGE, "DATA_TYPE", field, "ERROR",
                    message=(
                        f"'{field}' must be "
                        f"{'a whole number' if integer else 'numeric'}."
                    ),
                    recommended_action=(
                        f"Correct '{field}' in row {row_number}; remove "
                        f"text, symbols or decimals."
                    ),
                    table=table, row_number=row_number, row=row,
                    actual=value, expected=expected_type,
                )
                continue

            if (
                (minimum is not None and number < minimum)
                or (maximum is not None and number > maximum)
            ):

                collector.fail(
                    DATA_STAGE, "NUMERIC_RANGE", field, "ERROR",
                    message=f"'{field}' is outside the permitted range ({range_text}).",
                    recommended_action=(
                        f"Correct '{field}' in row {row_number} to a "
                        f"value {range_text}."
                    ),
                    table=table, row_number=row_number, row=row,
                    actual=number, expected=range_text,
                )


def check_dates(table, rules, collector, file_format) -> None:

    for field in rules.get("dates") or []:

        if not table.has(field):
            missing_column(collector, DATA_STAGE, "VALID_DATE", field)
            continue

        collector.check(DATA_STAGE, "VALID_DATE", field, "A valid date.")

        for row_number, row in table.rows:

            value = table.value(row, field)

            if is_blank(value):
                continue

            parsed, stored_as_text = to_date(value)

            if parsed is None:

                collector.fail(
                    DATA_STAGE, "VALID_DATE", field, "ERROR",
                    message=f"'{field}' is not a valid date.",
                    recommended_action=(
                        f"Enter a valid date (YYYY-MM-DD) for '{field}' "
                        f"in row {row_number}."
                    ),
                    table=table, row_number=row_number, row=row,
                    actual=value, expected="A valid date (YYYY-MM-DD).",
                )

            elif stored_as_text and file_format == "xlsx":

                collector.fail(
                    DATA_STAGE, "DATE_STORED_AS_TEXT", field, "WARNING",
                    message=(
                        f"'{field}' holds a valid date, but it is stored "
                        f"as text rather than an Excel date."
                    ),
                    recommended_action=(
                        "No action needed for loading; the value will be "
                        "converted. Ask the source system owner to export "
                        "dates as date cells."
                    ),
                    table=table, row_number=row_number, row=row,
                    actual=value, expected="An Excel date cell.",
                )


def check_allowed_values(table, rules, collector) -> None:

    for field, allowed in (rules.get("allowed_values") or {}).items():

        if not table.has(field):
            missing_column(collector, DATA_STAGE, "ALLOWED_VALUE", field)
            continue

        expected = f"One of {allowed}"

        collector.check(DATA_STAGE, "ALLOWED_VALUE", field, expected)

        for row_number, row in table.rows:

            value = table.value(row, field)

            if is_blank(value) or value_allowed(value, allowed):
                continue

            collector.fail(
                DATA_STAGE, "ALLOWED_VALUE", field, "ERROR",
                message=f"'{display(value)}' is not a permitted value for '{field}'.",
                recommended_action=(
                    f"Change '{field}' in row {row_number} to one of "
                    f"{allowed}, or add the value to the source registry "
                    f"if it is a legitimate new code."
                ),
                table=table, row_number=row_number, row=row,
                actual=value, expected=expected,
            )


def check_patterns(table, rules, collector) -> None:

    for field, pattern in (rules.get("patterns") or {}).items():

        if not table.has(field):
            missing_column(collector, DATA_STAGE, "PATTERN", field)
            continue

        compiled = re.compile(pattern)

        collector.check(DATA_STAGE, "PATTERN", field, pattern)

        for row_number, row in table.rows:

            value = table.value(row, field)

            if is_blank(value) or compiled.fullmatch(key_of(value)):
                continue

            collector.fail(
                DATA_STAGE, "PATTERN", field, "ERROR",
                message=f"'{display(value)}' does not match the format for '{field}'.",
                recommended_action=(
                    f"Correct '{field}' in row {row_number} to match "
                    f"the pattern {pattern}."
                ),
                table=table, row_number=row_number, row=row,
                actual=value, expected=pattern,
            )


def check_references(table, rules, collector, reference_data) -> None:

    for field, spec in (rules.get("references") or {}).items():

        if not table.has(field):
            missing_column(collector, DATA_STAGE, "REFERENTIAL_INTEGRITY", field)
            continue

        severity = spec.get("severity", "ERROR")

        _, target = reference_data.table(spec)
        target_text = f"{target}.{spec['field']}"

        valid_keys = reference_data.key_set(spec)

        if valid_keys is None:

            collector.fail(
                DATA_STAGE, "REFERENTIAL_INTEGRITY", field, "WARNING",
                message=(
                    f"Reference data {target_text} is not available, "
                    f"so '{field}' could not be checked."
                ),
                recommended_action=(
                    f"Ingest {target} (or add the config file) and "
                    f"re-run validation."
                ),
                expected=f"A value that exists in {target_text}.",
                actual="REFERENCE_DATA_UNAVAILABLE",
            )
            continue

        collector.check(
            DATA_STAGE, "REFERENTIAL_INTEGRITY", field,
            f"A value that exists in {target_text}.",
        )

        for row_number, row in table.rows:

            value = table.value(row, field)

            if is_blank(value) or key_of(value) in valid_keys:
                continue

            collector.fail(
                DATA_STAGE, "REFERENTIAL_INTEGRITY", field, severity,
                message=f"'{display(value)}' in '{field}' does not exist in {target_text}.",
                recommended_action=spec.get("recommended_action") or (
                    f"Correct '{field}' in row {row_number}, or add the "
                    f"missing record to {target} first."
                ),
                table=table, row_number=row_number, row=row,
                actual=value, expected=f"A value that exists in {target_text}.",
            )


# ---------------------------------------------------------
# BUSINESS RULES
# ---------------------------------------------------------

def check_calculations(table, rules, collector) -> None:

    for calculation in rules.get("calculations") or []:

        field = calculation["field"]
        formula = calculation["formula"]
        tolerance = calculation.get("tolerance", 0.01)
        severity = calculation.get("severity", "ERROR")
        rule = calculation.get("name", "CALCULATION")

        node, names = compile_formula(formula)

        absent = [name for name in [field, *names] if not table.has(name)]

        if absent:
            for name in absent:
                missing_column(collector, BUSINESS_STAGE, rule, name)
            continue

        expected_text = f"{field} = {formula} (± {tolerance})"

        collector.check(BUSINESS_STAGE, rule, field, expected_text)

        for row_number, row in table.rows:

            actual = to_number(table.value(row, field))

            values = {name: to_number(table.value(row, name)) for name in names}

            # Blank or non-numeric inputs are reported by the data rules.
            if actual is None or any(value is None for value in values.values()):
                continue

            try:
                expected = evaluate_formula(node, values)
            except ZeroDivisionError:
                continue

            if abs(actual - expected) > tolerance + 1e-9:

                collector.fail(
                    BUSINESS_STAGE, rule, field, severity,
                    message=(
                        f"'{field}' is {display(actual)} but {formula} "
                        f"= {round(expected, 2)} "
                        f"(difference {round(actual - expected, 2)})."
                    ),
                    recommended_action=calculation.get("recommended_action") or (
                        f"Recalculate '{field}' in row {row_number} or "
                        f"correct the input values ({', '.join(names)})."
                    ),
                    table=table, row_number=row_number, row=row,
                    actual=actual, expected=round(expected, 2),
                )


def check_comparisons(table, rules, collector, reference_data) -> None:

    for comparison in rules.get("comparisons") or []:

        field = comparison["field"]
        symbol = comparison["operator"]
        compare = COMPARISON_OPERATORS[symbol]
        severity = comparison.get("severity", "ERROR")
        rule = comparison.get("name", "COMPARISON")

        if not table.has(field):
            missing_column(collector, BUSINESS_STAGE, rule, field)
            continue

        lookup_spec = comparison.get("lookup")
        other_field = comparison.get("other_field")

        if lookup_spec:

            if not table.has(lookup_spec["key"]):
                missing_column(collector, BUSINESS_STAGE, rule, lookup_spec["key"])
                continue

            lookup = reference_data.lookup(lookup_spec)
            right_text = f"{lookup_spec['source']}.{lookup_spec['field']}"

            if lookup is None:

                collector.fail(
                    BUSINESS_STAGE, rule, field, "WARNING",
                    message=(
                        f"Reference data {right_text} is not available, "
                        f"so {rule} could not be checked."
                    ),
                    recommended_action=(
                        f"Ingest {lookup_spec['source']} and re-run validation."
                    ),
                    actual="REFERENCE_DATA_UNAVAILABLE",
                )
                continue

        elif other_field:

            if not table.has(other_field):
                missing_column(collector, BUSINESS_STAGE, rule, other_field)
                continue

            right_text = other_field

        else:

            right_text = str(comparison["value"])

        expected_text = f"{field} {symbol} {right_text}"

        collector.check(BUSINESS_STAGE, rule, field, expected_text)

        for row_number, row in table.rows:

            left = to_number(table.value(row, field))

            if lookup_spec:
                key = table.value(row, lookup_spec["key"])
                right = to_number(lookup.get(key_of(key))) if not is_blank(key) else None
            elif other_field:
                right = to_number(table.value(row, other_field))
            else:
                right = to_number(comparison["value"])

            # Missing inputs or unknown keys are reported by the data rules.
            if left is None or right is None or compare(left, right):
                continue

            collector.fail(
                BUSINESS_STAGE, rule, field, severity,
                message=(
                    f"{rule}: '{field}' is {display(left)}, which is not "
                    f"{symbol} {right_text} ({display(right)})."
                ),
                recommended_action=comparison.get("recommended_action") or (
                    f"Review row {row_number} so that {expected_text}."
                ),
                table=table, row_number=row_number, row=row,
                actual=left, expected=f"{symbol} {display(right)}",
            )


def check_group_balance(table, rules, collector) -> None:

    for balance in rules.get("group_balance") or []:

        group_field = balance["group_by"]
        debit_field = balance["debit"]
        credit_field = balance["credit"]
        tolerance = balance.get("tolerance", 0.01)
        severity = balance.get("severity", "ERROR")
        rule = balance.get("name", "GROUP_BALANCE")

        absent = [
            field
            for field in (group_field, debit_field, credit_field)
            if not table.has(field)
        ]

        if absent:
            for field in absent:
                missing_column(collector, BUSINESS_STAGE, rule, field)
            continue

        collector.check(
            BUSINESS_STAGE, rule, group_field,
            f"Total {debit_field} = total {credit_field} per {group_field}.",
        )
        collector.check(
            BUSINESS_STAGE, f"{rule}_FILE_TOTAL", None,
            f"Total {debit_field} = total {credit_field} for the file.",
        )

        debits = defaultdict(float)
        credits = defaultdict(float)
        first_row = {}

        for row_number, row in table.rows:

            group = table.value(row, group_field)

            if is_blank(group):
                continue

            group = key_of(group)

            debits[group] += to_number(table.value(row, debit_field)) or 0
            credits[group] += to_number(table.value(row, credit_field)) or 0
            first_row.setdefault(group, (row_number, row))

        for group in first_row:

            difference = debits[group] - credits[group]

            if abs(difference) > tolerance:

                row_number, row = first_row[group]

                collector.fail(
                    BUSINESS_STAGE, rule, group_field, severity,
                    message=(
                        f"{group_field} {group} does not balance: "
                        f"{debit_field} {round(debits[group], 2)} vs "
                        f"{credit_field} {round(credits[group], 2)} "
                        f"(difference {round(difference, 2)})."
                    ),
                    recommended_action=(
                        f"Correct or complete the lines of {group_field} "
                        f"{group} (starting at row {row_number}) so that "
                        f"debits equal credits."
                    ),
                    table=table, row_number=row_number, row=row,
                    actual=round(difference, 2), expected=0,
                )

        total_difference = sum(debits.values()) - sum(credits.values())

        if abs(total_difference) > tolerance:

            collector.fail(
                BUSINESS_STAGE, f"{rule}_FILE_TOTAL", None, severity,
                message=(
                    f"The file does not balance: total {debit_field} "
                    f"{round(sum(debits.values()), 2)} vs total "
                    f"{credit_field} {round(sum(credits.values()), 2)}."
                ),
                recommended_action=(
                    "Resolve the unbalanced groups listed above; if none "
                    "are listed, check for lines with a blank group key."
                ),
                actual=round(total_difference, 2), expected=0,
            )


# ---------------------------------------------------------
# FILE VALIDATION
# ---------------------------------------------------------

def has_row_rules(source_config: dict) -> bool:

    return bool(
        source_config.get("data_rules")
        or source_config.get("business_rules")
    )


def default_record_key(source_config: dict) -> list[str]:

    structure = source_config.get("structure") or {}

    if structure.get("record_key"):
        return list(structure["record_key"])

    unique_keys = (source_config.get("data_rules") or {}).get("unique") or []

    if unique_keys:
        first = unique_keys[0]
        return [first] if isinstance(first, str) else list(first)

    return []


def validate_data(
    file: Path,
    run_id: str,
    source: str,
    source_registry: dict,
    reference_data: ReferenceData,
    max_failures_per_rule: int = DEFAULT_MAX_FAILURES_PER_RULE,
) -> dict:
    """
    Run all data and business rules for one structurally valid file.
    """

    source_config = source_registry[source]
    structure = source_config.get("structure") or {}
    data_rules = source_config.get("data_rules") or {}
    business_rules = source_config.get("business_rules") or {}

    table = Table(*read_table(file, structure))

    collector = ResultCollector(
        run_id=run_id,
        source=source,
        file=file,
        sheet=structure.get("sheet"),
        record_key_fields=[
            field
            for field in default_record_key(source_config)
            if table.has(field)
        ],
        max_failures_per_rule=max_failures_per_rule,
    )

    file_format = source_config["format"].lower()

    check_required_values(table, data_rules, collector)
    check_unique(table, data_rules, collector)
    check_numeric(table, data_rules, collector)
    check_dates(table, data_rules, collector, file_format)
    check_allowed_values(table, data_rules, collector)
    check_patterns(table, data_rules, collector)
    check_references(table, data_rules, collector, reference_data)

    check_calculations(table, business_rules, collector)
    check_comparisons(table, business_rules, collector, reference_data)
    check_group_balance(table, business_rules, collector)

    results = collector.finalise(rows_checked=len(table.rows))

    # Count from the collector, not the logged rows, so rules whose
    # failures were capped are still counted in full.
    errors = sum(
        count for key, count in collector.failure_counts.items()
        if collector.failure_severity[key] == "ERROR"
    )
    warnings = sum(
        count for key, count in collector.failure_counts.items()
        if collector.failure_severity[key] == "WARNING"
    )

    if errors:
        status = "DATA_INVALID"
    elif warnings:
        status = "DATA_VALID_WITH_WARNINGS"
    else:
        status = "DATA_VALID"

    return {
        "run_id": run_id,
        "source": source,
        "file": file.name,
        "status": status,
        "rows_checked": len(table.rows),
        "errors": errors,
        "warnings": warnings,
        "failure_counts": collector.failure_counts,
        "failure_severity": collector.failure_severity,
        "results": results,
    }


def structure_gate_result(
    file: Path,
    run_id: str,
    source: str,
    structure_validation: dict,
) -> dict:

    return make_result(
        run_id=run_id,
        source=source,
        file=file,
        stage=DATA_STAGE,
        rule="STRUCTURE_GATE",
        status="FAIL",
        severity="ERROR",
        actual=structure_validation["status"],
        expected="STRUCTURE_VALID",
        message=(
            f"File failed structural validation "
            f"({structure_validation['failed_stage']}); data validation "
            f"was not performed."
        ),
        recommended_action=(
            "Run src/validation/validate_structure.py for details, "
            "fix the file and resubmit it."
        ),
    )


# ---------------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------------

def print_file_summary(validation: dict) -> None:

    line = (
        f"{validation['file']} -> {validation['source']} -> "
        f"{validation['status']} ({validation['rows_checked']} rows"
    )

    if validation["errors"]:
        line += f", {validation['errors']} error(s)"

    if validation["warnings"]:
        line += f", {validation['warnings']} warning(s)"

    print(line + ")")

    for (stage, rule, field), count in sorted(
        validation["failure_counts"].items(),
        key=lambda item: -item[1],
    ):
        severity = validation["failure_severity"][(stage, rule, field)]
        print(f"   {severity}: {rule} -> {field or '-'} x{count}")


def main() -> int:

    args = parse_arguments(
        "Validate the data quality and business rules of ingested source files."
    )

    source_registry = load_source_registry()

    max_failures = load_settings().get(
        "max_failures_per_rule",
        DEFAULT_MAX_FAILURES_PER_RULE,
    )

    run_id, targets, is_adhoc = select_targets(
        args.run_id,
        args.source,
        args.file,
        source_registry,
    )

    quarantine_enabled = not (is_adhoc or args.no_quarantine)

    reference_data = ReferenceData(read_manifest(), run_id, source_registry)

    print(f"\nDATA VALIDATION: {run_id}\n")

    status_counts = Counter()

    for source, file in targets:

        source_config = source_registry[source]

        if not has_row_rules(source_config):
            status_counts["NOT_APPLICABLE"] += 1
            continue

        structure_validation = validate_structure(
            file=file,
            run_id=run_id,
            source=source,
            source_registry=source_registry,
        )

        if structure_validation["status"] != "STRUCTURE_VALID":

            results = [structure_gate_result(file, run_id, source, structure_validation)]

            validation = {
                "file": file.name,
                "source": source,
                "status": "SKIPPED_STRUCTURE_INVALID",
                "rows_checked": 0,
                "errors": 1,
                "warnings": 0,
                "failure_counts": Counter({(DATA_STAGE, "STRUCTURE_GATE", None): 1}),
                "failure_severity": {(DATA_STAGE, "STRUCTURE_GATE", None): "ERROR"},
                "results": results,
            }

            quarantine_results = structure_validation["results"] + results

        else:

            validation = validate_data(
                file=file,
                run_id=run_id,
                source=source,
                source_registry=source_registry,
                reference_data=reference_data,
                max_failures_per_rule=max_failures,
            )

            results = validation["results"]
            quarantine_results = results

        write_validation_results(results)

        quarantine_path = None

        if validation["errors"] and quarantine_enabled:
            quarantine_path = quarantine_file(file, run_id, quarantine_results)

        write_file_status(
            run_id=run_id,
            source=source,
            file=file,
            stage=DATA_STAGE,
            status=validation["status"],
            results=results,
            rows_checked=validation["rows_checked"],
            quarantine_path=quarantine_path,
        )

        status_counts[validation["status"]] += 1

        print_file_summary(validation)

        if quarantine_path:
            print(f"   Quarantined: {quarantine_path.relative_to(PROJECT_ROOT)}")

    print("\nVALIDATION SUMMARY")
    print("--------------------------------------------")
    print(f"Run ID:                     {run_id}")
    print(f"Data valid:                 {status_counts['DATA_VALID']}")
    print(f"Data valid with warnings:   {status_counts['DATA_VALID_WITH_WARNINGS']}")
    print(f"Data invalid:               {status_counts['DATA_INVALID']}")
    print(f"Skipped (structure invalid): {status_counts['SKIPPED_STRUCTURE_INVALID']}")
    print(f"Not applicable (no rules):  {status_counts['NOT_APPLICABLE']}")
    print(f"Detailed results:           logs/validation_results.csv")

    invalid = (
        status_counts["DATA_INVALID"]
        + status_counts["SKIPPED_STRUCTURE_INVALID"]
    )

    return 1 if invalid else 0


if __name__ == "__main__":

    sys.exit(main())
