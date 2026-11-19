"""Lecture des CSV produits par le programme (séparateur détecté automatiquement)."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import csvio  # noqa: E402


def dict_rows(path):
    return csvio.read_rows(Path(path))


def list_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=csvio.detect_delimiter(Path(path))))
