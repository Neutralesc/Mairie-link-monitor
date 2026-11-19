"""Interface de lecture des « publications » (compte rendu, CSV et état du dernier contrôle par département).

L'interface web ne lit que cette abstraction. `FileSource` lit les fichiers du dépôt (reports/, resultats/) et la base
SQLite data/historique.sqlite ; une autre implémentation (base Postgres, déploiement Vercel) pourra la remplacer
sans toucher aux pages. Bibliothèque standard uniquement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import history
import report as report_module
from layout import DeptPaths

DEPT_RE = re.compile(r"^(\d{2,3}|2[AB])$")
CSV_TOUS = "liens_tous_departements.csv"


@dataclass(frozen=True)
class Publication:
    dept: str                 # '60', '2A'…
    controle_date: str        # ISO du dernier contrôle, '' si aucun
    communes: int             # nombre de communes du dernier contrôle (0 si aucun)
    avec_lien: int            # nombre de communes avec lien (0 si aucun)
    report_html: str          # compte rendu HTML complet (préfixe statique web par défaut)
    csv_bytes: bytes          # octets du CSV du département, tels quels (b'' s'il n'existe pas)
    csv_present: bool         # True si le fichier CSV existe (un fichier vide existe quand même)
    resume_changements: str   # ligne « Changements » de l'accueil ('' s'il n'y a aucun contrôle)


class PublicationSource(Protocol):
    def depts(self) -> list[str]: ...                    # codes triés
    # None si département inconnu ; report=False / csv=False évitent de produire les champs lourds (alors vides)
    def get(self, dept: str, *, report: bool = True, csv: bool = True) -> Publication | None: ...
    def csv_all(self) -> bytes | None: ...               # liens_tous_departements.csv, None s'il n'existe pas


class FileSource:
    """Implémentation actuelle : fichiers de la racine du projet et base SQLite data/historique.sqlite.

    Décision d'encodage : le CSV est servi en OCTETS, sans décodage ni réencodage (comme avant). Le BOM UTF-8 et les
    fins de ligne écrits par csvio sont donc conservés et la réponse HTTP reste identique octet pour octet.
    """

    def __init__(self, root: Path):
        self.root = root

    def depts(self) -> list[str]:
        return sorted(p.name for p in (self.root / "reports").glob("*") if p.is_dir() and DEPT_RE.match(p.name))

    def get(self, dept: str, *, report: bool = True, csv: bool = True) -> Publication | None:
        if dept not in self.depts():
            return None
        paths = DeptPaths(self.root, dept)
        db = history.db_path(self.root)
        ctl = history.controls(db, dept)
        cur = ctl[0] if ctl else None
        csv_present = paths.result_csv.exists()
        return Publication(
            dept=dept,
            controle_date=cur["date"] if cur else "",
            communes=cur["communes"] if cur else 0,
            avec_lien=cur["avec_lien"] if cur else 0,
            report_html=report_module.render(paths, db) if report else "",
            csv_bytes=paths.result_csv.read_bytes() if csv and csv_present else b"",
            csv_present=csv_present,
            resume_changements=history.summary_line(history.changes(db, dept)) if cur else "",
        )

    def csv_all(self) -> bytes | None:
        f = self.root / "resultats" / CSV_TOUS
        return f.read_bytes() if f.exists() else None
