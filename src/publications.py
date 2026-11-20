"""Interface de lecture des « publications » (compte rendu, CSV et état du dernier contrôle par département).

L'interface web ne lit que cette abstraction. `FileSource` lit les fichiers du dépôt (reports/, resultats/) et la base
SQLite data/historique.sqlite ; une autre implémentation (base Postgres, déploiement Vercel) pourra la remplacer
sans toucher aux pages. Bibliothèque standard uniquement.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import coverage
import history
import liens_vus
import report as report_module
import results
from layout import DeptPaths
from synthese import resume as resume_synthese
from synthese import synthese_departement

DEPT_RE = re.compile(r"^(\d{2,3}|2[AB])$")
CSV_TOUS = "liens_tous_departements.csv"


class SourceIndisponible(Exception):
    """La lecture des publications est impossible (base en panne) : l'interface répond 503, sans détail technique."""


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


def _sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn").casefold()


def trier_liens(rows: list[dict]) -> list[dict]:
    """Tri commun aux implémentations : dept, puis nom_commune (insensible à la casse ET aux accents : « Élancourt »
    se range avec les E), puis code_insee."""
    return sorted(rows, key=lambda r: (r["dept"], _sans_accents(r["nom_commune"]), r["nom_commune"].casefold(),
                                       r["code_insee"]))


def liens_confirmes(dept: str, synth) -> list[dict]:
    """Une entrée par lien confirmé d'un département, d'après une synthèse (synthese.synthese_departement).

    Seules les communes au statut « lien_confirme » comptent ; un lien écarté (verification « faux ») est exclu. Doublon
    (commune, page, cible) supprimé. Une synthèse vide ou mal formée donne une liste vide, sans erreur.
    """
    communes = synth.get("communes") if isinstance(synth, dict) else None
    sortie: list[dict] = []
    vus: set[tuple[str, str, str]] = set()
    for c in communes if isinstance(communes, list) else []:
        if not isinstance(c, dict) or c.get("statut") != "lien_confirme":
            continue
        code, nom = str(c.get("code_insee") or ""), str(c.get("nom") or "")
        liens = c.get("liens_confirmes")
        for lien in liens if isinstance(liens, list) else []:
            if not isinstance(lien, dict) or lien.get("verification") == "faux":
                continue
            page, cible = str(lien.get("url_page_source") or ""), str(lien.get("url_cible") or "")
            if not page or (code, page, cible) in vus:
                continue
            vus.add((code, page, cible))
            sortie.append({"dept": dept, "code_insee": code, "nom_commune": nom,
                           "url_page_source": page, "url_cible": cible})
    return sortie


def trier_liens_confirmes(rows: list[dict]) -> list[dict]:
    """Tri commun des liens confirmés : département, commune (sans accents ni casse), code INSEE, page, cible."""
    return sorted(rows, key=lambda r: (r["dept"], _sans_accents(r["nom_commune"]), r["nom_commune"].casefold(),
                                       r["code_insee"], r["url_page_source"], r["url_cible"]))


def selection_liens(rows: list[dict], q: str, limit: int, offset: int) -> tuple[list[dict], int]:
    """Filtre `q` (nom de commune ou code INSEE, sans accents ni casse), tri commun, puis tranche.

    Retourne (tranche, total des lignes filtrées). `limit` et `offset` sont des entiers positifs ou nuls.
    """
    cle = _sans_accents(q or "")
    if cle:
        rows = [r for r in rows if cle in _sans_accents(r["nom_commune"]) or cle in _sans_accents(r["code_insee"])]
    triees = trier_liens_confirmes(rows)
    return triees[offset:offset + limit], len(triees)


def selection_nouveaux(rows: list[dict], depuis: datetime, limit: int) -> tuple[list[dict], int]:
    """Liens nouveaux (nouveau et première vue >= depuis), du plus récent au plus ancien, puis département et commune."""
    retenus = [r for r in rows if r.get("nouveau") and datetime.fromisoformat(r["premiere_vue"]) >= depuis]
    triees = sorted(trier_liens_confirmes(retenus), key=lambda r: r["premiere_vue"], reverse=True)  # tri stable
    return triees[:limit], len(triees)


class PublicationSource(Protocol):
    def depts(self) -> list[str]: ...                    # codes triés
    # communes avec lien : {dept, code_insee, nom_commune, url_page}, triées ; dept=None = tous ; inconnu = []
    def liens(self, dept: str | None = None) -> list[dict]: ...
    # liens confirmés, une ligne par lien : {dept, code_insee, nom_commune, url_page_source, url_cible}. Filtre q
    # (commune ou code INSEE) et dept (None = tous), tri commun ; renvoie la tranche [offset, offset+limit[ et le total
    def tous_les_liens(self, q: str, dept: str | None, limit: int, offset: int) -> tuple[list[dict], int]: ...
    # liens nouvellement vus (nouveau = True, première vue >= depuis), plus récents d'abord : (tranche, total).
    # Chaque lien porte premiere_vue (ISO) et nouveau, comme dans tous_les_liens.
    def nouveaux_liens(self, depuis: datetime, limit: int) -> tuple[list[dict], int]: ...
    # None si département inconnu ; report=False / csv=False évitent de produire les champs lourds (alors vides)
    def get(self, dept: str, *, report: bool = True, csv: bool = True) -> Publication | None: ...
    def csv_all(self) -> bytes | None: ...               # liens_tous_departements.csv, None s'il n'existe pas
    def has_csv_all(self) -> bool: ...                   # existence du CSV global, sans en lire le contenu
    # synthèse par commune au format de l'ancien logiciel (synthese.synthese_departement) ; None si indisponible
    def synthese(self, dept: str) -> dict | None: ...
    def resumes(self) -> list[dict]: ...                 # un synthese.resume par département publié, trié par code
    # suivi des analyses : [{id, statut, departements, debut, fin}] du plus récent au plus ancien
    def analyses(self, limit: int = 50) -> list[dict]: ...
    def analyse(self, analysis_id: int) -> dict | None: ...  # + "journal" ; None si inconnue


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

    def liens(self, dept: str | None = None) -> list[dict]:
        """Communes avec lien, calculées comme dans report.render (mêmes coverage.build et results.collect)."""
        disponibles = self.depts()
        concernes = disponibles if dept is None else ([dept] if dept in disponibles else [])
        rows: list[dict] = []
        for d in concernes:
            paths = DeptPaths(self.root, d)
            states = coverage.build(paths) if paths.inventory_communes.exists() else []
            names = {s["code_insee"]: s["nom_commune"] for s in states}
            try:
                trouvees = results.collect(paths)
            except FileNotFoundError:
                continue
            rows.extend({"dept": d, "code_insee": r["code_insee"], "nom_commune": names.get(r["code_insee"], ""),
                         "url_page": r["url_page"]} for r in trouvees)
        return trier_liens(rows)

    def _lignes_publiees(self) -> list[dict]:
        """Tous les liens confirmés des départements, avec premiere_vue et nouveau.

        Enregistre au passage les liens inconnus dans data/historique.sqlite (table liens_vus, voir liens_vus.py) :
        c'est la seule écriture de cette source, idempotente, faite à la lecture faute de hook dans le crawl.
        """
        db = history.db_path(self.root)
        lignes: list[dict] = []
        bases: dict[str, str] = {}
        for d in self.depts():
            synth = self.synthese(d)
            if synth is None:
                continue
            lignes.extend(liens_confirmes(d, synth))
            ctl = history.controls(db, d)
            bases[d] = ctl[0]["date"] if ctl else ""
        maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
        vus = liens_vus.synchroniser(db, lignes, bases, maintenant)
        return [{**l, "premiere_vue": vus[liens_vus.cle(l)][0], "nouveau": vus[liens_vus.cle(l)][1]} for l in lignes]

    def tous_les_liens(self, q: str, dept: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
        """Liens confirmés des départements publiés (relus des synthèses à chaque appel : source locale, sans cache)."""
        lignes = self._lignes_publiees()
        if dept is not None:
            lignes = [l for l in lignes if l["dept"] == dept]
        return selection_liens(lignes, q, limit, offset)

    def nouveaux_liens(self, depuis: datetime, limit: int) -> tuple[list[dict], int]:
        return selection_nouveaux(self._lignes_publiees(), depuis, limit)

    def csv_all(self) -> bytes | None:
        f = self.root / "resultats" / CSV_TOUS
        return f.read_bytes() if f.exists() else None

    def has_csv_all(self) -> bool:
        return (self.root / "resultats" / CSV_TOUS).exists()

    def synthese(self, dept: str) -> dict | None:
        if dept not in self.depts():
            return None
        return synthese_departement(DeptPaths(self.root, dept), history.db_path(self.root))

    def resumes(self) -> list[dict]:
        db = history.db_path(self.root)
        sortie = []
        for d in self.depts():
            synth = synthese_departement(DeptPaths(self.root, d), db)
            if synth is not None:
                sortie.append(resume_synthese(synth))
        return sorted(sortie, key=lambda r: r["code"])

    def analyses(self, limit: int = 50) -> list[dict]:
        return []                       # pas de suivi des analyses hors base : le lancement passe par Postgres

    def analyse(self, analysis_id: int) -> dict | None:
        return None
