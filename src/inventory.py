#!/usr/bin/env python3
"""Lot 1 - Inventaire des communes d'un département et de leurs URL candidates (DILA).

Changements v0.3 par rapport à v0.2 :
  * Les mairies sont identifiées via le champ `pivot` (type_service_local == "mairie"),
    et non plus par une recherche de mots-clés (qui retenait 1382 enregistrements sur 1390).
  * Les URL sont extraites avec un statut explicite (url_non_verifiee / url_invalide) et
    jamais ignorées silencieusement.
  * Séparation récupération / transformation / écriture ; mode --offline pour rejouer
    la transformation à partir des CSV déjà téléchargés.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import csvio
from layout import DeptPaths, normalize_dept

log = logging.getLogger("inventory_oise")

COG_URL = "https://www.insee.fr/fr/statistiques/fichier/8740222/v_commune_2026.csv"
DILA_API = ("https://api-lannuaire.service-public.gouv.fr/api/explore/v2.1/catalog/datasets/"
            "api-lannuaire-administration/records")
USER_AGENT = "MairieLinkMonitor/0.6 (inventory audit; configure contact before production)"
PAGE_SIZE = 100
EXPECTED_COM = {"60": 680}  # contrôle de cohérence connu (Oise) ; pas de valeur de référence pour les autres

# Statuts URL
URL_NON_VERIFIEE = "url_non_verifiee"
URL_INVALIDE = "url_invalide"
# Statuts commune
SITE_TROUVE = "site_trouve"
PLUSIEURS_SITES = "plusieurs_sites_candidats"
SANS_URL = "enregistrement_sans_url"
AUCUN_ENREGISTREMENT = "aucun_enregistrement_dila"
AMBIGU = "donnee_ambigue"
TOUTES_URL_INVALIDES = "url_invalide"


# --------------------------------------------------------------------------- récupération
def fetch_bytes(url: str, timeout: int = 30, attempts: int = 3, backoff: float = 2.0) -> bytes:
    """GET avec nouvelles tentatives sur erreurs serveur (5xx) et erreurs réseau ; 4xx = échec immédiat."""
    for attempt in range(1, attempts + 1):
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urlopen(req, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            retryable = exc.code >= 500
            log.warning("HTTP %s sur %s (tentative %d/%d)", exc.code, url, attempt, attempts)
            if not retryable or attempt == attempts:
                raise
        except (URLError, TimeoutError) as exc:
            log.warning("Erreur réseau sur %s : %s (tentative %d/%d)", url, exc, attempt, attempts)
            if attempt == attempts:
                raise
        time.sleep(backoff * attempt)
    raise RuntimeError("unreachable")


def fetch_json(url: str, timeout: int = 30) -> dict:
    return json.loads(fetch_bytes(url, timeout).decode("utf-8"))


def read_cog(text: str) -> list[dict]:
    """Toutes les lignes du COG national, clés en MAJUSCULES."""
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=";,\t").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if not reader.fieldnames:
        raise RuntimeError("Le CSV COG ne contient pas d'en-têtes.")
    rows = [{str(k).strip().upper(): (v.strip() if isinstance(v, str) else v)
             for k, v in original.items() if k is not None} for original in reader]
    if rows and not {"TYPECOM", "COM", "LIBELLE", "DEP"}.issubset(rows[0]):
        raise RuntimeError(f"Colonnes COG inattendues : {list(rows[0])}")
    return rows


def cog_departments(rows: list[dict]) -> list[str]:
    return sorted({normalize_dept(r["DEP"]) for r in rows if r.get("TYPECOM") == "COM" and r.get("DEP")})


def select_dept(rows: list[dict], dept: str) -> tuple[list[dict], list[dict], dict[str, str]]:
    """(toutes lignes du département, communes de type COM, {code arrondissement: code commune parente})."""
    dept_rows = [r for r in rows if normalize_dept(r.get("DEP", "")) == dept]
    communes = [r for r in dept_rows if r.get("TYPECOM") == "COM"]
    arm_parent = {r["COM"]: r["COMPARENT"] for r in dept_rows if r.get("TYPECOM") == "ARM" and r.get("COMPARENT")}
    return dept_rows, communes, arm_parent


def fetch_dila_records(dept: str) -> list[dict]:
    records: list[dict] = []
    offset = 0
    log.info("Interrogation de l'API DILA (département %s)…", dept)
    while True:
        params = {"where": f'startswith(code_insee_commune,"{dept}")', "limit": PAGE_SIZE, "offset": offset}
        data = fetch_json(f"{DILA_API}?{urlencode(params)}")
        batch = data.get("results")
        if not isinstance(batch, list):
            raise RuntimeError(f"Réponse API inattendue : clés reçues {list(data.keys())}")
        records.extend(batch)
        log.info("  %d enregistrements récupérés…", len(records))
        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
        time.sleep(0.25)
    return records


def read_csv_rows(path: Path) -> list[dict]:
    return csvio.read_rows(path)


# --------------------------------------------------------------------------- transformation
def flatten_values(value):
    """Extrait récursivement les valeurs texte, y compris celles encodées en JSON dans une chaîne."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("[", "{")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                yield value  # conservé : sera signalé url_invalide en aval, pas ignoré
            else:
                yield from flatten_values(parsed)
        else:
            yield value
    elif isinstance(value, dict):
        if "valeur" in value:
            yield from flatten_values(value["valeur"])
        else:
            for nested in value.values():
                yield from flatten_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from flatten_values(nested)


@dataclass(frozen=True)
class UrlCandidate:
    url_origine: str
    url_candidate: str
    statut_url: str
    motif_statut: str


def classify_url(raw: str) -> UrlCandidate:
    """Normalisation prudente : strip, '//' -> https:, validation http(s). Pas de réécriture hasardeuse."""
    candidate = raw.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if any(ch.isspace() for ch in candidate):
        return UrlCandidate(raw, candidate, URL_INVALIDE, "espaces ou texte dans la valeur")
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ("http", "https"):
        return UrlCandidate(raw, candidate, URL_INVALIDE, "schéma http/https absent ou non supporté")
    if not parts.netloc or "." not in parts.netloc:
        return UrlCandidate(raw, candidate, URL_INVALIDE, "nom de domaine absent ou incomplet")
    return UrlCandidate(raw, candidate, URL_NON_VERIFIEE,
                        "présent dans la DILA ; accessibilité et caractère officiel non vérifiés")


def extract_url_candidates(value) -> list[UrlCandidate]:
    """Toutes les valeurs non vides de `site_internet`, valides ou non, sans doublon exact."""
    out: list[UrlCandidate] = []
    seen: set[str] = set()
    for item in flatten_values(value):
        if not isinstance(item, str) or not item.strip():
            continue
        cand = classify_url(item)
        if cand.url_candidate in seen:
            continue
        seen.add(cand.url_candidate)
        out.append(cand)
    return out


def extract_site_urls(record: dict) -> list[str]:
    """Compatibilité v0.2 : seulement les URL valides."""
    return [c.url_candidate for c in extract_url_candidates(record.get("site_internet"))
            if c.statut_url != URL_INVALIDE]


def parse_pivot(record: dict) -> list[dict]:
    """`pivot` est une chaîne JSON : [{"type_service_local": ..., "code_insee_commune": [...]}]."""
    pivot = record.get("pivot")
    if isinstance(pivot, str):
        if not pivot.strip():
            return []
        try:
            pivot = json.loads(pivot)
        except json.JSONDecodeError:
            log.warning("pivot illisible pour %r", record.get("nom"))
            return []
    return [p for p in (pivot or []) if isinstance(p, dict)]


def service_type(record: dict) -> str:
    """'mairie', 'mairie_deleguee', ou le type_service_local brut (ccas, gendarmerie_departementale…)."""
    types = [p.get("type_service_local") or "" for p in parse_pivot(record)]
    if "mairie" in types:
        nom = str(record.get("nom") or "").casefold()
        if nom.startswith("mairie déléguée"):
            return "mairie_deleguee"
        return "mairie_annexe" if nom.startswith("mairie annexe") else "mairie"
    return types[0] if types and types[0] else "inconnu"


def record_commune_codes(record: dict) -> list[str]:
    codes: list[str] = []
    for p in parse_pivot(record):
        for c in p.get("code_insee_commune") or []:
            if str(c) not in codes:
                codes.append(str(c))
    fallback = str(record.get("code_insee_commune") or "").strip()
    if not codes and fallback:
        codes.append(fallback)
    return codes


def build_inventory(communes: list[dict], dila_records: list[dict], collected_at: str,
                    arm_parent: dict[str, str] | None = None):
    """Retourne (lignes_communes, lignes_urls, enregistrements_mairie_hors_cog).
    arm_parent : codes d'arrondissements (Paris, Lyon, Marseille) -> commune parente."""
    arm_parent = arm_parent or {}
    cog_codes = {str(c["COM"]).strip() for c in communes}
    mairies_by_code: dict[str, list[tuple[dict, str]]] = {}
    orphans: list[dict] = []
    for rec in dila_records:
        stype = service_type(rec)
        if stype not in ("mairie", "mairie_deleguee", "mairie_annexe"):
            continue
        matched = []
        for c in record_commune_codes(rec):
            c = arm_parent.get(c, c)
            if c in cog_codes and c not in matched:
                matched.append(c)
        if not matched:
            orphans.append(rec)
        for code in matched:
            mairies_by_code.setdefault(code, []).append((rec, stype))

    communes_rows, url_rows = [], []
    for commune in communes:
        code = str(commune["COM"]).strip()
        entries = mairies_by_code.get(code, [])
        principals = [e for e in entries if e[1] == "mairie"]
        base = {"code_insee": code, "nom_commune": commune.get("LIBELLE", ""),
                "departement": commune.get("DEP", ""), "type_commune": commune.get("TYPECOM", ""),
                "date_collecte": collected_at}
        seen_urls: dict[str, dict] = {}
        for rec, stype in entries:
            for cand in extract_url_candidates(rec.get("site_internet")):
                if cand.url_candidate in seen_urls:
                    continue
                row = {**base, "nom_enregistrement_dila": rec.get("nom", ""), "type_service": stype,
                       "url_candidate": cand.url_candidate, "url_origine": cand.url_origine,
                       "source_url": rec.get("url_service_public", ""), "id_dila": rec.get("id", ""),
                       "statut_url": cand.statut_url, "motif_statut": cand.motif_statut}
                seen_urls[cand.url_candidate] = row
                url_rows.append(row)
        valid = [r for r in seen_urls.values() if r["statut_url"] == URL_NON_VERIFIEE]
        invalid = [r for r in seen_urls.values() if r["statut_url"] == URL_INVALIDE]
        if not entries:
            status, motif = AUCUN_ENREGISTREMENT, "aucun enregistrement de type mairie dans la DILA pour ce code"
        elif len(principals) > 1:
            status, motif = AMBIGU, f"{len(principals)} enregistrements 'mairie' pour ce code"
        elif len(valid) == 1:
            status, motif = SITE_TROUVE, "1 URL candidate (non vérifiée)"
        elif len(valid) > 1:
            status, motif = PLUSIEURS_SITES, f"{len(valid)} URL candidates distinctes"
        elif invalid:
            status, motif = TOUTES_URL_INVALIDES, "seules des valeurs invalides dans site_internet"
        else:
            status, motif = SANS_URL, "enregistrement mairie sans site_internet (≠ la commune n'a pas de site)"
        if not seen_urls:
            url_rows.append({**base, "nom_enregistrement_dila": entries[0][0].get("nom", "") if entries else "",
                             "type_service": entries[0][1] if entries else "", "url_candidate": "",
                             "url_origine": "", "source_url": "", "id_dila": "", "statut_url": "",
                             "motif_statut": motif})
        communes_rows.append({**base, "statut_commune": status, "motif_statut": motif,
                              "nb_enregistrements_mairie": len(entries),
                              "nb_urls_valides": len(valid), "nb_urls_invalides": len(invalid),
                              "urls_candidates": " | ".join(r["url_candidate"] for r in valid),
                              "urls_invalides": " | ".join(r["url_origine"] for r in invalid)})
    return communes_rows, url_rows, orphans


def build_summary(communes_rows, url_rows, communes, dila_records, orphans, collected_at, dept: str = "") -> dict:
    statuts: dict[str, int] = {}
    for r in communes_rows:
        statuts[r["statut_commune"]] = statuts.get(r["statut_commune"], 0) + 1
    types: dict[str, int] = {}
    for rec in dila_records:
        t = service_type(rec)
        types[t] = types.get(t, 0) + 1
    valid_urls = {r["url_candidate"] for r in url_rows if r["statut_url"] == URL_NON_VERIFIEE}
    return {
        "version": "0.6", "generated_at_utc": collected_at, "department": dept,
        "cog_source": COG_URL, "dila_api": DILA_API,
        "communes_cog_type_com": len(communes), "communes_cog_attendu": EXPECTED_COM.get(dept),
        "dila_records_retrieved": len(dila_records),
        "dila_records_par_type_service_top10": dict(sorted(types.items(), key=lambda kv: -kv[1])[:10]),
        "dila_mairie_records": types.get("mairie", 0),
        "dila_mairie_deleguee_records": types.get("mairie_deleguee", 0),
        "dila_mairie_annexe_records": types.get("mairie_annexe", 0),
        "dila_mairie_records_hors_cog": len(orphans),
        "communes_avec_enregistrement_mairie": sum(r["nb_enregistrements_mairie"] > 0 for r in communes_rows),
        "communes_avec_url_candidate": sum(r["nb_urls_valides"] > 0 for r in communes_rows),
        "communes_sans_url_referencee": sum(r["nb_urls_valides"] == 0 for r in communes_rows),
        "urls_distinctes": len(valid_urls),
        "cas_ambigus_ou_en_erreur": sum(r["statut_commune"] in (AMBIGU, TOUTES_URL_INVALIDES)
                                        for r in communes_rows),
        "urls_invalides": sum(r["statut_url"] == URL_INVALIDE for r in url_rows),
        "statuts_communes": statuts,
        "limitations": [
            "Identification des mairies via pivot.type_service_local == 'mairie' ; validée sur l'Oise (60) uniquement ; arrondissements (Paris/Lyon/Marseille), communes nouvelles, Corse et outre-mer non vérifiés.",
            "Une URL candidate n'est ni confirmée officielle ni vérifiée accessible (lot 2).",
            "Plusieurs URL pour une mairie peuvent inclure des sites tiers, intercommunaux ou de démarches (lot 2).",
            "'Aucune URL dans la DILA' ne signifie pas que la commune n'a pas de site web.",
            "Les URL des autres services (CCAS, etc.) ne sont pas utilisées pour les communes sans URL mairie.",
            "Aucun crawl de site de mairie n'est effectué dans ce lot.",
        ],
    }


# --------------------------------------------------------------------------- écriture
def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    csvio.write_rows(path, rows, fields)


def ensure_cog(root: Path, refresh: bool = False, cached_only: bool = False) -> list[dict]:
    """Lit le COG national depuis le cache local ; le télécharge sinon (sauf cached_only)."""
    path = root / "data" / "v_commune_2026.csv"
    if path.exists() and not refresh:
        return read_cog(path.read_bytes().decode("utf-8-sig"))
    if cached_only:
        raise RuntimeError(f"{path} absent : téléchargez le COG une première fois (sans --use-cached-cog).")
    log.info("Téléchargement du COG INSEE 2026…")
    payload = fetch_bytes(COG_URL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return read_cog(payload.decode("utf-8-sig"))


def run(paths: DeptPaths, offline: bool = False, use_cached_cog: bool = False,
        cog_rows: list[dict] | None = None) -> int:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    dept = paths.dept
    collected_at = datetime.now(timezone.utc).isoformat()
    try:
        if offline:
            log.info("[%s] Mode hors-ligne : lecture de %s", dept, paths.data_dir)
            all_rows = read_csv_rows(paths.cog_all_types)
            dept_rows, communes, arm_parent = select_dept(all_rows, dept)
            dila_records = read_csv_rows(paths.dila_records)
        else:
            rows = cog_rows if cog_rows is not None else ensure_cog(paths.root, cached_only=use_cached_cog)
            dept_rows, communes, arm_parent = select_dept(rows, dept)
            if not communes:
                raise RuntimeError(f"Aucune commune de type COM pour le département {dept} dans le COG.")
            dila_records = fetch_dila_records(dept)
            write_csv(paths.cog_all_types, dept_rows)
            write_csv(paths.cog_communes, communes)
            write_csv(paths.dila_records, dila_records)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
        log.error("[%s] %s", dept, exc)
        log.error("[%s] Import incomplet : ne pas tirer de conclusion de couverture.", dept)
        return 1

    expected = EXPECTED_COM.get(dept)
    if expected and len(communes) != expected:
        log.warning("[%s] COG : %d communes COM, %d attendues - à investiguer.", dept, len(communes), expected)

    communes_rows, url_rows, orphans = build_inventory(communes, dila_records, collected_at, arm_parent)
    summary = build_summary(communes_rows, url_rows, communes, dila_records, orphans, collected_at, dept)
    write_csv(paths.inventory_communes, communes_rows)
    write_csv(paths.inventory_urls, url_rows)
    write_csv(paths.inventory_orphans,
              [{"nom": o.get("nom"), "pivot": o.get("pivot"), "site_internet": o.get("site_internet")}
               for o in orphans], ["nom", "pivot", "site_internet"])
    paths.inventory_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[{dept}] Inventaire {'(hors-ligne) ' if offline else ''}terminé.")
    for label, key in [("Communes COG de type COM", "communes_cog_type_com"),
                       ("Enregistrements DILA", "dila_records_retrieved"),
                       ("Enregistrements mairie (pivot)", "dila_mairie_records"),
                       ("Communes avec enregistrement mairie", "communes_avec_enregistrement_mairie"),
                       ("Communes avec URL candidate", "communes_avec_url_candidate"),
                       ("Communes sans URL référencée", "communes_sans_url_referencee"),
                       ("URL distinctes", "urls_distinctes"),
                       ("Cas ambigus / en erreur", "cas_ambigus_ou_en_erreur"),
                       ("URL invalides", "urls_invalides")]:
        print(f"  {label} : {summary[key]}")
    print("  Statuts :", summary["statuts_communes"])
    return 0
