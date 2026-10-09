"""
Pipeline runner with a colour-coded status board.

Runs each pipeline stage in order, shows its live progress, exit code
and outcome, and writes the full output of every stage to
logs/pipeline_<timestamp>.log.

    python scripts/run_pipeline.py               # asks before regenerating data
    python scripts/run_pipeline.py --regenerate  # regenerate without asking
    python scripts/run_pipeline.py --keep-data   # never regenerate, no question
    python scripts/run_pipeline.py --skip-tests

Press Ctrl+C at any time to cancel. You can let the running step finish
before stopping (safe), stop it immediately, or carry on.

Exit codes: 0 = success, 1 = completed with warnings, 2 = failed,
130 = cancelled.

Double-click run_pipeline.bat in the project root to open this in a
separate terminal window.
"""

import argparse
import csv
import os
import re
import signal
import subprocess
import sys
import threading
import time

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

INCOMING_DIR = PROJECT_ROOT / "data" / "incoming"
LOG_DIR = PROJECT_ROOT / "logs"
RUN_LOG_FILE = LOG_DIR / "ingestion_runs.csv"
FILE_STATUS_LOG_FILE = LOG_DIR / "validation_file_status.csv"

CANCELLED_EXIT_CODE = 130


# ---------------------------------------------------------
# CONSOLE COLOURS
# ---------------------------------------------------------

def enable_ansi_colours() -> None:
    """Turn on ANSI colour support in the classic Windows console."""

    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()

        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)

    except Exception:
        pass


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

COLOURS = {
    "SUCCESS": "\033[92m",    # green
    "WARNING": "\033[93m",    # yellow
    "FAILED": "\033[91m",     # red
    "ERROR": "\033[91m",      # red
    "CANCELLED": "\033[95m",  # magenta
    "SKIPPED": "\033[90m",    # grey
    "RUNNING": "\033[96m",    # cyan
    "QUESTION": "\033[96m",   # cyan
}

STATUS_EXIT_CODES = {
    "SUCCESS": 0,
    "SKIPPED": 0,
    "WARNING": 1,
    "FAILED": 2,
    "ERROR": 2,
}


def coloured(status: str, text: str | None = None) -> str:

    return f"{COLOURS.get(status, '')}{BOLD}{text or status}{RESET}"


def clear_line() -> None:

    print("\r" + " " * 90 + "\r", end="", flush=True)


# ---------------------------------------------------------
# USER INTERACTION
# ---------------------------------------------------------

def is_interactive() -> bool:

    return sys.stdin is not None and sys.stdin.isatty()


def ask(question: str, choices: dict[str, str], default: str) -> str:
    """
    Ask a single-letter question. choices maps letter -> meaning.
    Returns the chosen letter; the default if input is not interactive.
    """

    if not is_interactive():
        return default

    options = "/".join(
        letter.upper() if letter == default else letter
        for letter in choices
    )

    while True:

        try:
            answer = input(f"  {coloured('QUESTION', '?')} {question} [{options}] ")
        except EOFError:
            # No input available (on Windows NUL counts as a console).
            print()
            return default

        answer = answer.strip().lower()

        if not answer:
            return default

        if answer[0] in choices:
            return answer[0]

        print(f"    Please answer one of: {', '.join(choices)}")


def ask_how_to_cancel(label: str) -> str:
    """
    Called when Ctrl+C is pressed during a step. Returns 'wait',
    'stop' or 'continue'. A second Ctrl+C at the prompt stops at once.
    """

    clear_line()
    print(f"\n  {coloured('CANCELLED', 'Cancel requested')} while running '{label}'.")
    print(f"    {BOLD}w{RESET} = wait for this step to finish, then stop (safest)")
    print(f"    {BOLD}s{RESET} = stop this step now")
    print(f"    {BOLD}c{RESET} = continue the pipeline")

    try:
        choice = ask(
            "What would you like to do?",
            {"w": "wait", "s": "stop", "c": "continue"},
            default="w",
        )
    except (KeyboardInterrupt, EOFError):
        print()
        return "stop"

    print()

    return {"w": "wait", "s": "stop", "c": "continue"}[choice]


# ---------------------------------------------------------
# LOG HELPERS
# ---------------------------------------------------------

def read_csv_rows(path: Path) -> list[dict]:

    if not path.exists():
        return []

    with open(path, "r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def new_rows(path: Path, rows_before: int) -> list[dict]:

    return read_csv_rows(path)[rows_before:]


class PipelineLog:

    def __init__(self) -> None:

        LOG_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.path = LOG_DIR / f"pipeline_{timestamp}.log"

    def section(self, title: str, output: str) -> None:

        with open(self.path, "a", encoding="utf-8") as log:
            log.write(f"\n{'=' * 70}\n{title}\n{'=' * 70}\n{output}\n")


# ---------------------------------------------------------
# STEP EXECUTION
# ---------------------------------------------------------

@dataclass
class CommandRun:
    exit_code: int | None
    output: str
    seconds: float
    stopped: bool = False          # killed on request
    cancel_after: bool = False     # finished, but the user asked to stop after it


def kill_process_tree(process: subprocess.Popen) -> None:
    """
    Stop a step and any processes it started. On Windows the venv
    python.exe is a launcher that starts the real interpreter as a
    child, so the whole tree must be stopped.
    """

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        os.killpg(process.pid, signal.SIGTERM)

    process.wait()


def run_command(label: str, arguments: list[str]) -> CommandRun:
    """
    Run a pipeline script with a live elapsed-time indicator.

    The script runs in its own process group so Ctrl+C reaches only
    this runner, which then asks what to do instead of killing the
    script mid-write.
    """

    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    isolation = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )

    started = time.perf_counter()

    process = subprocess.Popen(
        [sys.executable, *arguments],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        **isolation,
    )

    output_parts = []

    # Read output on a background thread so a chatty script never
    # blocks on a full pipe while we update the progress line.
    reader = threading.Thread(
        target=lambda: output_parts.append(process.stdout.read()),
        daemon=True,
    )
    reader.start()

    cancel_after = False
    stopped = False

    while process.poll() is None:

        try:

            elapsed = time.perf_counter() - started

            note = "  (stopping after this step)" if cancel_after else ""

            print(
                f"\r  {label:<28} {coloured('RUNNING', f'{"RUNNING":<11}')} "
                f"{'':<8} {DIM}{elapsed:5.0f}s{note}{RESET}   ",
                end="",
                flush=True,
            )

            time.sleep(0.5)

        except KeyboardInterrupt:

            choice = ask_how_to_cancel(label)

            if choice == "stop":
                print(f"  Stopping '{label}'...")
                kill_process_tree(process)
                stopped = True
                break

            if choice == "wait":
                cancel_after = True

    reader.join(timeout=5)

    return CommandRun(
        exit_code=process.returncode,
        output="".join(output_parts),
        seconds=time.perf_counter() - started,
        stopped=stopped,
        cancel_after=cancel_after,
    )


# ---------------------------------------------------------
# STEP INTERPRETERS
# ---------------------------------------------------------
# Each returns (status, detail) from the step's exit code, output
# and the log rows it wrote.

def interpret_generation(exit_code: int, output: str) -> tuple[str, str]:

    if exit_code != 0:
        return "ERROR", "Data generator crashed - see the pipeline log."

    files = re.search(r"files_created: (\d+)", output)
    balanced = re.search(r"gl_balanced: (\w+)", output)

    detail = f"{files.group(1) if files else '?'} files created"

    if balanced and balanced.group(1) != "True":
        return "WARNING", detail + "; generated GL does not balance"

    return "SUCCESS", detail + "; GL balanced"


def interpret_ingestion(exit_code: int, rows: list[dict]) -> tuple[str, str]:

    if exit_code != 0 or not rows:
        return "ERROR", "Ingestion crashed - see the pipeline log."

    run = rows[-1]

    detail = (
        f"{run['run_id']}: {run['ingested']} ingested, "
        f"{run['duplicates']} duplicates, {run['rejected']} rejected, "
        f"{run['unregistered']} unregistered"
    )

    status = {
        "COMPLETED": "SUCCESS",
        "COMPLETED_WITH_WARNINGS": "WARNING",
        "COMPLETED_WITH_ERRORS": "FAILED",
    }.get(run["run_status"], "WARNING")

    if status == "SUCCESS" and run["ingested"] == "0":
        detail += " (nothing new - re-validating latest batch)"

    return status, detail


def interpret_validation(
    exit_code: int,
    rows: list[dict],
    valid_statuses: set[str],
    warning_statuses: set[str],
) -> tuple[str, str]:

    # Exit code 1 means files failed validation; anything else
    # non-zero means the script itself failed.
    if exit_code not in (0, 1) or not rows:
        return "ERROR", "Validation crashed - see the pipeline log."

    counts = Counter(row["status"] for row in rows)

    valid = sum(counts[status] for status in valid_statuses)
    warnings = sum(counts[status] for status in warning_statuses)
    invalid = len(rows) - valid - warnings

    detail = (
        f"{rows[-1]['run_id']}: {valid} valid, "
        f"{warnings} with warnings, {invalid} invalid"
    )

    if invalid:
        quarantined = sum(1 for row in rows if row.get("quarantine_path"))
        return "FAILED", detail + f" ({quarantined} quarantined)"

    if warnings:
        return "WARNING", detail

    return "SUCCESS", detail


def interpret_tests(exit_code: int, output: str) -> tuple[str, str]:

    summary = output.strip().splitlines()[-1] if output.strip() else ""
    summary = summary.strip("= ").strip()

    if exit_code == 0:
        return "SUCCESS", summary

    if exit_code == 1:
        return "FAILED", summary

    return "ERROR", summary or "pytest could not run - see the pipeline log."


# What a step stopped part-way through leaves behind, and what to do.
STOPPED_NOTES = {
    "1. Data generation": (
        "Stopped part-way. Files are replaced only once complete, but some "
        "may be new and some old - answer 'y' to regenerate on the next run."
    ),
    "2. Ingestion": (
        "Stopped part-way. Files already copied are recorded in the manifest; "
        "the next run ingests the rest."
    ),
    "3. Structural validation": (
        "Stopped part-way. Results for this run are incomplete - run the "
        "pipeline again to re-validate."
    ),
    "4. Data validation": (
        "Stopped part-way. Results for this run are incomplete - run the "
        "pipeline again to re-validate."
    ),
    "5. Automated tests": "Stopped part-way. No project data is affected.",
}


# ---------------------------------------------------------
# STATUS BOARD
# ---------------------------------------------------------

def print_step_result(
    label: str,
    status: str,
    exit_code: int | None,
    seconds: float | None,
    detail: str,
) -> None:

    code = "-" if exit_code is None else str(exit_code)
    duration = "" if seconds is None else f"{seconds:5.0f}s"

    clear_line()
    print(
        # Pad before colouring: escape codes would otherwise count
        # towards the column width and break the alignment.
        f"  {label:<28} {coloured(status, f'{status:<11}')} "
        f"{'exit ' + code:<8} {DIM}{duration:>6}{RESET}"
    )
    print(f"  {DIM}{'':<28} {detail}{RESET}")


def print_header() -> None:

    print()
    print(f"  {BOLD}RETAIL DATA PLATFORM v2 - PIPELINE STATUS{RESET}")
    print(f"  {DIM}Nawa Retail Group | {datetime.now():%Y-%m-%d %H:%M:%S}{RESET}")
    print(f"  {DIM}Press Ctrl+C at any time to cancel safely.{RESET}")
    print(f"  {'-' * 74}")
    print(f"  {'STEP':<28} {'STATUS':<11} {'CODE':<8} {'TIME':>6}")
    print(f"  {'-' * 74}")


def print_legend() -> None:

    print(
        f"  Legend: {coloured('SUCCESS')}  {coloured('WARNING')}  "
        f"{coloured('FAILED')}  {coloured('ERROR')}  "
        f"{coloured('CANCELLED')}  {coloured('SKIPPED')}"
    )


# ---------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------

class PipelineCancelled(Exception):
    """Raised when the user cancels; remaining steps are not run."""


class Pipeline:

    def __init__(self, args: argparse.Namespace) -> None:

        self.args = args
        self.log = PipelineLog()
        self.results = []
        self.blocked_by = None  # label of a crashed step

    def record(self, label, status, exit_code, seconds, detail) -> None:

        self.results.append(status)
        print_step_result(label, status, exit_code, seconds, detail)

    def execute(self, label: str, arguments: list[str]) -> CommandRun:
        """
        Run a script. Raises PipelineCancelled (after recording the step)
        if the user stopped it or asked to stop after it.
        """

        run = run_command(label, arguments)

        self.log.section(
            f"{label} (exit {run.exit_code}{', stopped by user' if run.stopped else ''})",
            run.output,
        )

        if run.stopped:
            self.record(
                label, "CANCELLED", None, run.seconds,
                STOPPED_NOTES.get(label, "Stopped part-way."),
            )
            raise PipelineCancelled

        return run

    def blocked(self, label: str) -> bool:

        if self.blocked_by:
            self.record(
                label, "SKIPPED", None, None,
                f"Skipped because '{self.blocked_by}' failed.",
            )
            return True

        return False

    def finish_step(self, label, status, run, detail) -> None:

        self.record(label, status, run.exit_code, run.seconds, detail)

        if status == "ERROR":
            self.blocked_by = label

        if run.cancel_after:
            raise PipelineCancelled

    # -----------------------------------------------------
    # STEPS
    # -----------------------------------------------------

    def generate_data(self) -> None:

        label = "1. Data generation"

        if any(INCOMING_DIR.glob("*.xlsx")):

            if self.args.regenerate:
                regenerate = True

            elif self.args.keep_data:
                regenerate = False

            else:
                print(f"  {label:<28} Synthetic data already exists in data/incoming/.")
                print(
                    f"  {'':<28} {DIM}New data replaces it and is ingested as a "
                    f"new batch.{RESET}"
                )
                regenerate = ask(
                    "Generate new data?", {"y": "yes", "n": "no"}, default="n",
                ) == "y"

            if not regenerate:
                self.record(
                    label, "SKIPPED", None, None,
                    "Kept the existing synthetic data.",
                )
                return

        run = self.execute(label, ["scripts/generate_all.py"])
        status, detail = interpret_generation(run.exit_code, run.output)
        self.finish_step(label, status, run, detail)

    def ingest(self) -> None:

        label = "2. Ingestion"

        if self.blocked(label):
            return

        rows_before = len(read_csv_rows(RUN_LOG_FILE))

        run = self.execute(label, ["src/ingestion/discover_files.py"])
        status, detail = interpret_ingestion(
            run.exit_code, new_rows(RUN_LOG_FILE, rows_before),
        )
        self.finish_step(label, status, run, detail)

    def validate(self, label, script, valid_statuses, warning_statuses) -> None:

        if self.blocked(label):
            return

        rows_before = len(read_csv_rows(FILE_STATUS_LOG_FILE))

        run = self.execute(label, [script])
        status, detail = interpret_validation(
            run.exit_code,
            new_rows(FILE_STATUS_LOG_FILE, rows_before),
            valid_statuses,
            warning_statuses,
        )
        self.finish_step(label, status, run, detail)

    def test(self) -> None:

        label = "5. Automated tests"

        if self.args.skip_tests:
            self.record(label, "SKIPPED", None, None, "Skipped (--skip-tests).")
            return

        # Tests use their own fixtures, so they run even if an
        # earlier step failed.
        run = self.execute(label, ["-m", "pytest", "tests", "-q"])
        status, detail = interpret_tests(run.exit_code, run.output)
        self.finish_step(label, status, run, detail)

    def steps(self) -> list[tuple[str, callable]]:

        return [
            ("1. Data generation", self.generate_data),
            ("2. Ingestion", self.ingest),
            ("3. Structural validation", lambda: self.validate(
                "3. Structural validation",
                "src/validation/validate_structure.py",
                {"STRUCTURE_VALID"},
                set(),
            )),
            ("4. Data validation", lambda: self.validate(
                "4. Data validation",
                "src/validation/validate_data.py",
                {"DATA_VALID"},
                {"DATA_VALID_WITH_WARNINGS"},
            )),
            ("5. Automated tests", self.test),
        ]

    # -----------------------------------------------------
    # RUN
    # -----------------------------------------------------

    def run(self) -> int:

        print_header()

        cancelled = False
        steps = self.steps()

        for index, (label, step) in enumerate(steps):

            try:
                step()

            except (PipelineCancelled, KeyboardInterrupt, EOFError) as reason:

                # KeyboardInterrupt here means Ctrl+C at a question or
                # between steps, when nothing is running. The console
                # echoes ^C without a newline, so start a fresh line.
                if not isinstance(reason, PipelineCancelled):
                    print()

                if len(self.results) <= index:
                    self.record(label, "CANCELLED", None, None, "Cancelled before it started.")

                for remaining_label, _ in steps[index + 1:]:
                    self.record(
                        remaining_label, "CANCELLED", None, None,
                        "Not run - pipeline cancelled.",
                    )

                cancelled = True
                break

        self.record(
            "6. SQL staging & warehouse", "SKIPPED", None, None,
            "Not implemented yet (roadmap phases 7-8).",
        )

        if cancelled:
            overall, exit_code = "CANCELLED", CANCELLED_EXIT_CODE
        else:
            exit_code = max(STATUS_EXIT_CODES[status] for status in self.results)
            overall = {0: "SUCCESS", 1: "WARNING", 2: "FAILED"}[exit_code]

        print(f"  {'-' * 74}")
        print(f"  {BOLD}PIPELINE STATUS:{RESET} {coloured(overall)}   exit code {exit_code}")

        if cancelled:
            print(
                f"  {DIM}Cancelled safely. Completed steps are kept; run the "
                f"pipeline again to continue.{RESET}"
            )

        if self.log.path.exists():
            print(f"  {DIM}Full output: {self.log.path.relative_to(PROJECT_ROOT)}{RESET}")

        print(f"  {DIM}Details:     logs/validation_results.csv, logs/validation_file_status.csv{RESET}")
        print()
        print_legend()
        print()

        return exit_code


def main() -> int:

    parser = argparse.ArgumentParser(description="Run the data pipeline.")

    data_choice = parser.add_mutually_exclusive_group()
    data_choice.add_argument(
        "--regenerate",
        action="store_true",
        help="Regenerate the synthetic data without asking.",
    )
    data_choice.add_argument(
        "--keep-data",
        action="store_true",
        help="Keep the existing synthetic data without asking.",
    )

    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Do not run the automated tests.",
    )

    args = parser.parse_args()

    enable_ansi_colours()

    try:
        return Pipeline(args).run()
    except KeyboardInterrupt:
        # Ctrl+C while the summary was printing.
        print(f"\n  {coloured('CANCELLED')}")
        return CANCELLED_EXIT_CODE


if __name__ == "__main__":

    sys.exit(main())
