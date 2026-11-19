"""Comparaison avec d'anciens résultats : pourquoi une commune qui avait un lien n'en a plus (ou autrement).

Entrée : un ou plusieurs anciens fichiers de liens (ex. links_found_oise.csv d'une version précédente) ;
         les fichiers actuels reports/<dep>/links_found.csv, crawl_sites.csv, url_checks.csv.
Sortie : reports/<dep>/comparaison_ancienne_version.csv + résumé affiché.
"""
from __future__ import annotations

from pathlib import Path

import csvio
from layout import DeptPaths

OUT_FIELDS = ["code_insee", "nom_commune", "pages_anciennes", "pages_actuelles", "verdict", "cause", "statut_crawl",
              "motif", "pages_visitees", "limite_pages_atteinte", "limite_profondeur_atteinte", "pages_tronquees",
              "tentatives", "erreur"]


def _pages_by_commune(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        code = (r.get("code_insee") or "").strip()
        page = (r.get("page_url") or r.get("url_page") or "").strip()
        if code and page:
            d = out.setdefault(code, {"nom": r.get("nom_commune", ""), "pages": set()})
            d["pages"].add(page)
            d["nom"] = d["nom"] or r.get("nom_commune", "")
    return out


def _cause(site: dict | None, check: dict | None) -> str:
    if site:
        st = site.get("statut_crawl", "")
        if st == "erreur_acces":
            return "site illisible lors du dernier passage (erreur réseau ou d'accès)"
        if st in ("robots_interdit", "robots_indisponible"):
            return "exploration refusée ou impossible (robots.txt)"
        if st == "explore_partiel":
            return "exploration limitée (pages, profondeur, erreurs ou pages tronquées) : le lien a pu ne pas être atteint"
        if st == "explore_complet":
            return "site entièrement lu sans lien : le lien n'y est plus (ou il est chargé en JavaScript)"
        if st == "non_explore":
            return f"site non exploré ({site.get('motif', '')})"
        return f"statut {st}"
    if check:
        return (f"site non exploré : vérification des URL = {check.get('statut_acces')}, "
                f"confiance = {check.get('confiance')}")
    return "commune absente des fichiers actuels (étapes inventory/verify/crawl non exécutées pour elle)"


def compare(paths: DeptPaths, old_files: list[Path]) -> list[dict]:
    old = _pages_by_commune([r for f in old_files for r in csvio.read_rows(f)])
    new = _pages_by_commune(csvio.read_rows(paths.links_found)) if paths.links_found.exists() else {}
    sites: dict[str, dict] = {}
    if paths.crawl_sites.exists():
        for r in csvio.read_rows(paths.crawl_sites):
            if r["code_insee"] not in sites or r.get("statut_crawl") != "non_explore":
                sites[r["code_insee"]] = r
    checks = {r["code_insee"]: r for r in csvio.read_rows(paths.url_checks)} if paths.url_checks.exists() else {}
    rows = []
    for code in sorted(old):
        old_pages, now = old[code]["pages"], new.get(code, {"pages": set()})["pages"]
        if now & old_pages:
            verdict, cause = "retrouvée (même page)", ""
        elif now:
            verdict, cause = "retrouvée (autre page)", "le lien est maintenant trouvé sur une autre page du site"
        else:
            verdict, cause = "NON retrouvée", _cause(sites.get(code), checks.get(code))
        s = sites.get(code) or {}
        rows.append({"code_insee": code, "nom_commune": old[code]["nom"], "pages_anciennes": " | ".join(sorted(old_pages)),
                     "pages_actuelles": " | ".join(sorted(now)), "verdict": verdict, "cause": cause,
                     **{k: s.get(k, "") for k in OUT_FIELDS[6:]}})
    csvio.write_rows(paths.reports_dir / "comparaison_ancienne_version.csv", rows, OUT_FIELDS)
    return rows


def report(paths: DeptPaths, old_files: list[Path]) -> int:
    for f in old_files:
        if not f.exists():
            print(f"Fichier introuvable : {f}")
            return 1
    old_rows = [r for f in old_files for r in csvio.read_rows(f)]
    old = _pages_by_commune(old_rows)
    new = _pages_by_commune(csvio.read_rows(paths.links_found)) if paths.links_found.exists() else {}
    rows = compare(paths, old_files)
    n_old_pages = sum(len(v["pages"]) for v in old.values())
    n_new_pages = sum(len(v["pages"]) for v in new.values())
    print(f"\n[{paths.dept}] Comparaison avec l'ancienne version")
    print(f"  Ancienne : {n_old_pages} page(s) distincte(s) pour {len(old)} commune(s)")
    print(f"  Actuelle : {n_new_pages} page(s) distincte(s) pour {len(new)} commune(s) (fichier links_found.csv)")
    print("  → Le CSV final (une page par commune) contient une ligne par COMMUNE : "
          f"{len(new)} ligne(s), même si une commune a plusieurs pages.")
    verdicts: dict[str, int] = {}
    causes: dict[str, int] = {}
    for r in rows:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
        if r["cause"] and r["verdict"].startswith("NON"):
            causes[r["cause"]] = causes.get(r["cause"], 0) + 1
    for v, n in sorted(verdicts.items()):
        print(f"  {n:4d} commune(s) : {v}")
    for c, n in sorted(causes.items(), key=lambda kv: -kv[1]):
        print(f"      dont {n} : {c}")
    print(f"  Détail commune par commune : {paths.reports_dir / 'comparaison_ancienne_version.csv'}")
    return 0
