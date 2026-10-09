import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The pipeline scripts are run directly (python src/...), so they import
# their siblings by module name. Mirror that for the tests.
sys.path.insert(0, str(PROJECT_ROOT / "src" / "ingestion"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "validation"))
