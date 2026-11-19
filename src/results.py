"""Export minimal : code INSEE + URL de la page où le lien vers le site cible a été trouvé.

Mode par défaut « une_page_par_commune » : un lien présent dans le menu ou le pied de page d'un site apparaît sur
toutes ses pages ; ce n'est qu'UN emplacement. On retient alors une seule page par commune, de préférence la page
d'accueil. Le mode « toutes_les_pages » liste chaque page où le lien a été vu.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

import csvio
from layout import DeptPaths

log = logging.getLogger("results")
RESULT_FIELDS = ["code_insee", "url_page"]
CLICKABLE_TYPES = {"lien", "iframe", "lien_attribut", "lien_script"}
MODES = ("une_page_par_commune", "toutes_les_pages")
_CIBLE_RANK = {"oui": 0, "generique": 1, "": 1, None: 1, "autre_commune": 2}


def _page_key(url: str) -> str:
    p = urlsplit(url)
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}"


def _depth(url: str) -> int:
    return urlsplit(url).path.rstrip("/").count("/")


def pick_page(rows: list[dict]) -> dict:
    """Meilleure page d'une commune : cible propre à la commune > page d'accueil du site > chemin le plus court."""
    def key(r: dict):
        home = _page_key(r["page_url"]) == _page_key(r.get("site_url") or "")
        platform = (r.get("confiance_site") or "").startswith("plateforme")      # le site de la mairie d'abord
        return (_CIBLE_RANK.get(r.get("cible_commune"), 1), platform, 0 if home else 1, _depth(r["page_url"]), r["page_url"])
    return min(rows, key=key)


def collect(paths: DeptPaths, include_text: bool = False, mode: str = "une_page_par_commune") -> list[dict]:
    """[{code_insee, url_page}] uniques et triés, d'après links_found.csv et liens_plateformes.csv."""
    if mode not in MODES:
        raise ValueError(f"mode d'export inconnu : {mode} (valides : {', '.join(MODES)})")
    sources = [f for f in (paths.links_found, paths.platform_links) if f.exists()]
    if not sources:
        raise FileNotFoundError(f"{paths.links_found} introuvable : l'étape d'exploration n'a pas été exécutée "
                                f"pour le département {paths.dept}.")
    allowed = CLICKABLE_TYPES | ({"texte"} if include_text else set())
    by_commune: dict[str, list[dict]] = {}
    for r in (row for f in sources for row in csvio.read_rows(f)):
        if r.get("type_lien", "lien") in allowed and r.get("code_insee") and r.get("page_url"):
            by_commune.setdefault(r["code_insee"].strip(), []).append({**r, "page_url": r["page_url"].strip()})
    out: set[tuple[str, str]] = set()
    for code, rows in by_commune.items():
        if mode == "une_page_par_commune":
            out.add((code, pick_page(rows)["page_url"]))
        else:
            out.update((code, r["page_url"]) for r in rows)
    return [{"code_insee": c, "url_page": u} for c, u in sorted(out)]


def write_result(path: Path, rows: list[dict], delimiter: str | None = None) -> None:
    csvio.write_rows(path, rows, RESULT_FIELDS, delimiter)


def export_dept(paths: DeptPaths, include_text: bool = False, delimiter: str | None = None,
                mode: str = "une_page_par_commune") -> int:
    rows = collect(paths, include_text, mode)
    write_result(paths.result_csv, rows, delimiter)
    print(f"[{paths.dept}] {len(rows)} ligne(s), {len({r['code_insee'] for r in rows})} commune(s) avec lien "
          f"({'1 page par commune' if mode == 'une_page_par_commune' else 'toutes les pages'}) -> {paths.result_csv}")
    return len(rows)


def export_all(root: Path, include_text: bool = False, delimiter: str | None = None,
               mode: str = "une_page_par_commune") -> Path | None:
    """Fichier cumulé de tous les départements déjà explorés (reports/<dep>/links_found.csv)."""
    rows: list[dict] = []
    depts = {f.parent.name for pattern in ("*/links_found.csv", "*/liens_plateformes.csv") for f in (root / "reports").glob(pattern)}
    for d in sorted(depts):
        rows.extend(collect(DeptPaths(root, d), include_text, mode))
    if not rows:
        return None
    out = root / "resultats" / "liens_tous_departements.csv"
    write_result(out, sorted(rows, key=lambda r: (r["code_insee"], r["url_page"])), delimiter)
    print(f"Cumul tous départements : {len(rows)} ligne(s) -> {out}")
    return out
