"""Lecture/écriture CSV centralisées.

Écriture : séparateur « ; » par défaut (Excel français ouvre alors les colonnes séparément ; avec « , » tout se
retrouvait dans une seule cellule et Excel transformait la ligne entière en un faux lien hypertexte).
Lecture : séparateur détecté automatiquement à partir de la ligne d'en-tête (anciens fichiers « , » toujours lisibles).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

DELIMITER = ";"


def set_delimiter(value: str) -> None:
    global DELIMITER
    if value not in (",", ";", "\t"):
        raise ValueError(f"séparateur CSV non supporté : {value!r}")
    DELIMITER = value


def detect_delimiter(path: Path) -> str:
    with path.open(encoding="utf-8-sig", newline="") as f:
        header = f.readline()
    counts = {d: header.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else DELIMITER


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=detect_delimiter(path)))


def write_rows(path: Path, rows: list[dict], fields: list[str] | None = None, delimiter: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", delimiter=delimiter or DELIMITER)
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                        for k, v in row.items()})
    tmp.replace(path)


def append_row(path: Path, row: dict, fields: list[str], delimiter: str | None = None) -> None:
    """Ajoute une ligne (écrit l'en-tête si le fichier est nouveau) ; utilisé pour les journaux de performance."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", delimiter=delimiter or DELIMITER)
        if new:
            w.writeheader()
        w.writerow(row)
