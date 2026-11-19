"""Couverture : combien de communes sont réellement analysées, et où les autres sont perdues.

Une commune est « analysée » si la page d'accueil d'au moins un de ses sites a pu être lue. Pour chaque commune du
COG : son état et la raison. Sortie : reports/<dep>/couverture.csv + entonnoir affiché.
"""
from __future__ import annotations

import csvio
from layout import DeptPaths

FIELDS = ["code_insee", "nom_commune", "etat", "analysee", "statut_inventaire", "nb_urls_dila", "statut_crawl", "motif",
          "url_utilisee", "confiance_finale"]
READ_OK = {"lien_trouve", "explore_complet", "explore_partiel"}


def build(paths: DeptPaths) -> list[dict]:
    communes = csvio.read_rows(paths.inventory_communes)
    sites: dict[str, list[dict]] = {}
    if paths.crawl_sites.exists():
        for r in csvio.read_rows(paths.crawl_sites):
            sites.setdefault(r["code_insee"], []).append(r)
    platform: dict[str, list[dict]] = {}
    if paths.platform_pages.exists():
        for r in csvio.read_rows(paths.platform_pages):
            if r.get("code_insee"):
                platform.setdefault(r["code_insee"], []).append(r)
    rows = []
    for c in communes:
        code = c["code_insee"]
        mine = sites.get(code, [])
        pp = platform.get(code, [])
        best = next((r for r in mine if r["statut_crawl"] == "lien_trouve"), None) \
            or next((r for r in mine if r["statut_crawl"] in READ_OK), None) or (mine[0] if mine else {})
        statut = best.get("statut_crawl", "")
        if statut == "lien_trouve":
            etat = "lien trouvé"
        elif statut == "explore_complet":
            etat = "site lu en entier, aucun lien"
        elif statut == "explore_partiel":
            etat = "site lu partiellement, aucun lien (exploration limitée)"
        elif mine and statut:
            etat = f"site non lu : {statut}"
        elif int(c.get("nb_urls_valides") or 0) == 0:
            etat = "aucun site référencé (DILA) et aucun site trouvé"
        else:
            etat = "non traitée (étape d'exploration non exécutée)"
        if statut != "lien_trouve" and any(int(r.get("liens_trouves") or 0) for r in pp):
            etat = "lien trouvé (PanneauPocket)"
        elif statut not in READ_OK and any(r.get("statut") == "lue" for r in pp):
            etat = "page PanneauPocket lue, aucun lien"
        read_pp = any(r.get("statut") == "lue" for r in pp)
        rows.append({"code_insee": code, "nom_commune": c["nom_commune"], "etat": etat,
                     "analysee": "oui" if statut in READ_OK or read_pp else "non", "statut_inventaire": c.get("statut_commune", ""),
                     "nb_urls_dila": c.get("nb_urls_valides", ""), "statut_crawl": statut, "motif": best.get("motif", ""),
                     "url_utilisee": best.get("url_utilisee", ""), "confiance_finale": best.get("confiance_finale", "")})
    return rows


def report(paths: DeptPaths, quiet: bool = False) -> list[dict]:
    if not paths.inventory_communes.exists():
        return []
    rows = build(paths)
    csvio.write_rows(paths.coverage, rows, FIELDS)
    if not quiet:
        n = len(rows)
        count = lambda pred: sum(1 for r in rows if pred(r))  # noqa: E731
        analysed = count(lambda r: r["analysee"] == "oui")
        print(f"\n[{paths.dept}] Couverture de l'analyse")
        print(f"  {n:5d} communes (COG)")
        print(f"  {count(lambda r: r['statut_crawl'] != ''):5d} avec au moins un site tenté "
              f"({count(lambda r: r['etat'].startswith('aucun site')):d} sans aucun site référencé ni trouvé)")
        print(f"  {analysed:5d} analysées (page d'accueil lue) = {100 * analysed / n:.0f} % des communes" if n else "")
        print(f"        dont {count(lambda r: r['etat'].startswith('lien trouvé')):d} avec lien trouvé, "
              f"{count(lambda r: r['etat'].startswith('site lu en entier')):d} lues en entier sans lien, "
              f"{count(lambda r: r['etat'].startswith('site lu partiellement')):d} lues partiellement sans lien")
        print(f"        dont {count(lambda r: r['etat'].endswith('(PanneauPocket)')):d} lien(s) sur PanneauPocket, "
              f"{count(lambda r: r['etat'].startswith('page PanneauPocket')):d} commune(s) sans site lue(s) sur PanneauPocket")
        print(f"  {count(lambda r: r['etat'].startswith('site non lu')):5d} sites non lus (erreur d'accès, robots.txt…)")
        print(f"  Détail commune par commune : {paths.coverage}")
    return rows
