#!/usr/bin/env python3
"""Lot 2 - Fiabilisation des URL candidates : accessibilité, redirections, niveau de confiance.

Entrée  : reports/<dep>/inventory_urls.csv (lot 1)
Sorties : reports/<dep>/url_checks.csv  (une ligne par couple commune/URL)
          reports/<dep>/url_checks_summary.json

Principes :
  * une seule requête GET par URL distincte (corps non lu), redirections suivies et tracées ;
  * politesse : concurrence globale limitée + délai minimal entre deux requêtes vers un même hôte ;
  * aucune certitude affichée : `confiance` est une heuristique documentée, jamais une preuve ;
  * les erreurs sont enregistrées avec leur type, jamais transformées en valeur vide.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

import csvio
from layout import DeptPaths

log = logging.getLogger("verify_urls")
UA_BASE = "MairieLinkMonitor/0.6 (verification d'URL)"

# ----------------------------------------------------------------- listes de référence (heuristiques)
# Plateformes d'hébergement / constructeurs de sites : le site peut être celui de la mairie, mais l'origine est à vérifier.
PLATEFORMES = ("e-monsite.com", "jimdofree.com", "jimdosite.com", "wixsite.com", "wix.com", "canalblog.com",
               "blogspot.com", "over-blog.com", "free.fr", "wordpress.com", "weebly.com", "webnode.fr",
               "neopse-site.com", "collectivite.fr", "sites.google.com", "numairique.fr")
# Annuaires et sites manifestement tiers.
ANNUAIRES = ("lapagelocale.fr", "annuaire-mairie.fr", "communes.com", "linternaute.com", "ville-data.com")
# Institutions et services d'Etat / départementaux : jamais le site d'une commune.
INSTITUTIONS = ("gouv.fr", "service-public.fr", "oise.fr", "ameli.fr", "caf.fr", "impots.gouv.fr", "justice.fr",
                "ac-amiens.fr", "francetravail.fr", "urssaf.fr", "msa.fr", "banque-france.fr", "ordre.medecin.fr",
                "conseil-national.medecin.fr", "oise.cci.fr")
STOPWORDS = {"le", "la", "les", "l", "de", "du", "des", "d", "en", "sur", "sous", "aux", "au", "et", "a", "lès", "les"}
RETRY_STATUTS = {"timeout", "http_5xx", "erreur_connexion", "bloque_ou_limite", "erreur_autre"}

FIELDS = ["code_insee", "nom_commune", "type_service", "url_candidate", "http_status", "statut_acces",
          "final_url", "nb_redirections", "type_redirection", "content_type", "duree_ms", "erreur",
          "confiance", "motif_confiance", "nb_communes_meme_url", "date_controle"]


# ----------------------------------------------------------------- fonctions pures (testées)
def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def registrable(host: str) -> str:
    """Approximation sans liste publique des suffixes : 2 derniers labels (suffisant pour .fr/.com ;
    imparfait pour les suffixes de type .co.uk, sans objet ici)."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _slug(text: str) -> str:
    text = text.replace("œ", "oe").replace("Œ", "oe").replace("æ", "ae").replace("Æ", "ae")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def commune_keys(name: str) -> set[str]:
    """Formes compactes du nom de commune comparables à un nom de domaine."""
    slug = _slug(name)
    tokens = slug.split()
    sig = [t for t in tokens if t not in STOPWORDS]
    no_article = tokens[1:] if tokens and tokens[0] in {"le", "la", "les", "l"} else tokens
    keys = {"".join(tokens), "".join(sig), "".join(no_article)}
    keys |= {k.replace("saint", "st") for k in list(keys)}
    return {k for k in keys if len(k) >= 3}


def domain_matches_commune(host: str, name: str) -> bool:
    core = host.rsplit(".", 1)[0] if "." in host else host
    compact = re.sub(r"[^a-z0-9]", "", core)
    tokens = set(re.split(r"[^a-z0-9]+", core))
    # clé >= 4 caractères : sous-chaîne du domaine ; clé courte (3) : doit être un mot entier du domaine
    return any((k in compact) if len(k) >= 4 else (k in tokens) for k in commune_keys(name))


def redirect_type(original: str, final: str) -> str:
    if not final or original.rstrip("/") == final.rstrip("/"):
        return "aucune"
    o, f = urlsplit(original), urlsplit(final)
    oh, fh = (o.hostname or "").lower(), (f.hostname or "").lower()
    if registrable(oh.removeprefix("www.")) != registrable(fh.removeprefix("www.")):
        return "changement_domaine"
    kinds = []
    if o.scheme != f.scheme:
        kinds.append(f"{o.scheme}_vers_{f.scheme}")
    if oh != fh:
        kinds.append("changement_sous_domaine_ou_www")
    if not kinds:
        kinds.append("meme_domaine_chemin_different")
    return "+".join(kinds)


def classify_status(status: int | None, exc: Exception | None) -> str:
    if exc is not None:
        if isinstance(exc, httpx.TooManyRedirects):
            return "trop_de_redirections"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        text = f"{type(exc).__name__} {exc}".lower()
        if "ssl" in text or "certificate" in text:
            return "erreur_ssl"
        if "name or service not known" in text or "getaddrinfo" in text or "nodename" in text \
                or "no address associated" in text or "temporary failure in name resolution" in text:
            return "erreur_dns"
        if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.NetworkError)):
            return "erreur_connexion"
        return "erreur_autre"
    if status is None:
        return "erreur_autre"
    if 200 <= status < 300:
        return "accessible"
    if status in (401, 403, 429):
        return "bloque_ou_limite"
    if 400 <= status < 500:
        return "http_4xx"
    if status >= 500:
        return "http_5xx"
    return "erreur_autre"  # 1xx / 3xx non suivie


def _exact_domain_match(host: str, name: str) -> bool:
    """Le cœur du domaine (sans mairie/commune/ville/de…) est égal à une forme du nom de la commune."""
    core = re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0])
    core = re.sub(r"^(mairiede|mairie|communede|commune|villede|ville)", "", core)
    return core in commune_keys(name)


def assess_confidence(commune: str, original_url: str, final_url: str | None, statut_acces: str,
                      n_communes_same_url: int, redirection: str = "aucune") -> tuple[str, str]:
    """Retourne (confiance, motif). Heuristique : aucune valeur ne vaut confirmation officielle."""
    urls = [original_url] + ([final_url] if final_url else [])
    hosts = [host_of(u) for u in urls]
    paths = " ".join(urlsplit(u).path.lower() for u in urls)
    for h in hosts:
        if any(h == d or h.endswith("." + d) for d in INSTITUTIONS):
            return "institution_non_communale", f"domaine {h} : institution ou service non communal"
        if any(h == d or h.endswith("." + d) for d in ANNUAIRES):
            return "tiers_probable", f"domaine {h} : annuaire ou site tiers"
    if n_communes_same_url > 1:
        return "intercommunal_ou_service_probable", f"même URL référencée pour {n_communes_same_url} communes"
    if any(re.search(r"(^|[-.])(cc|ccas|comcom|agglo|communaute)", h) for h in hosts) \
            or re.search(r"/(ccas|centre-communal)", paths):
        return "intercommunal_ou_service_probable", "domaine ou chemin évoquant CC/CCAS/agglomération"
    if statut_acces != "accessible":
        return "inaccessible", f"non joignable lors du contrôle ({statut_acces})"
    if any(any(h == d or h.endswith("." + d) for d in PLATEFORMES) for h in hosts):
        return "plateforme_tierce_a_verifier", "site hébergé sur une plateforme généraliste : origine à vérifier"
    final_host = hosts[-1]
    if redirection == "changement_domaine" and not _exact_domain_match(final_host, commune):
        return "a_verifier", (f"redirigé vers un autre domaine ({final_host}) qui n'est pas strictement"
                              " celui de la commune (intercommunalité, prestataire ?)")
    if domain_matches_commune(final_host, commune):
        return "probable_officiel", "domaine cohérent avec le nom de la commune (heuristique, non prouvé)"
    return "a_verifier", "domaine sans lien évident avec le nom de la commune"


# ----------------------------------------------------------------- réseau
class HostThrottle:
    """Garantit un délai minimal entre deux requêtes de départ vers un même hôte."""

    def __init__(self, delay: float):
        self.delay, self._locks, self._last = delay, {}, {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = self.delay - (time.monotonic() - self._last.get(host, -1e9))
            if gap > 0:
                await asyncio.sleep(gap)
            self._last[host] = time.monotonic()


async def check_one(client: httpx.AsyncClient, url: str, throttle: HostThrottle, sem: asyncio.Semaphore) -> dict:
    async with sem:
        await throttle.wait(host_of(url))
        started = time.monotonic()
        status, final, exc, ctype, hops = None, "", None, "", 0
        try:
            async with client.stream("GET", url) as resp:   # en-têtes seulement : corps non lu
                status, final = resp.status_code, str(resp.url)
                ctype = resp.headers.get("content-type", "")
                hops = len(resp.history)
        except Exception as e:  # noqa: BLE001 - classée puis enregistrée, jamais ignorée
            exc = e
        statut = classify_status(status, exc)
        return {"http_status": status if status is not None else "", "statut_acces": statut,
                "final_url": final, "nb_redirections": hops, "content_type": ctype.split(";")[0],
                "duree_ms": int((time.monotonic() - started) * 1000),
                "erreur": f"{type(exc).__name__}: {exc}"[:200] if exc else ""}


async def check_all(urls: list[str], concurrency: int, host_delay: float, timeout: float, max_redirects: int,
                    user_agent: str, transport: httpx.AsyncBaseTransport | None = None,
                    on_progress=None) -> dict[str, dict]:
    sem, throttle = asyncio.Semaphore(concurrency), HostThrottle(host_delay)
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=max_redirects, timeout=timeout,
                                 headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.5"},
                                 transport=transport) as client:
        async def work(u: str):
            results[u] = await check_one(client, u, throttle, sem)
            if on_progress:
                on_progress(len(results), len(urls), u, results[u])
        await asyncio.gather(*(work(u) for u in urls))
    return results


# ----------------------------------------------------------------- orchestration / E-S
def load_input(path: Path) -> list[dict]:
    return [r for r in csvio.read_rows(path) if r.get("statut_url") == "url_non_verifiee" and r.get("url_candidate")]


def load_previous(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    return {(r["code_insee"], r["url_candidate"]): r for r in csvio.read_rows(path)}


def build_rows(inputs: list[dict], results: dict[str, dict], checked_at: str) -> list[dict]:
    counts: dict[str, set] = {}
    for r in inputs:
        counts.setdefault(r["url_candidate"], set()).add(r["code_insee"])
    rows = []
    for r in inputs:
        res = results[r["url_candidate"]]
        n_same = len(counts[r["url_candidate"]])
        redir = redirect_type(r["url_candidate"], res["final_url"])
        conf, motif = assess_confidence(r["nom_commune"], r["url_candidate"], res["final_url"] or None,
                                        res["statut_acces"], n_same, redir)
        rows.append({"code_insee": r["code_insee"], "nom_commune": r["nom_commune"],
                     "type_service": r.get("type_service", ""), "url_candidate": r["url_candidate"], **res,
                     "type_redirection": redir,
                     "confiance": conf, "motif_confiance": motif,
                     "nb_communes_meme_url": n_same, "date_controle": res.get("date_controle", checked_at)})
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    csvio.write_rows(path, rows, FIELDS)


def summarize(rows: list[dict], checked_at: str, params: dict) -> dict:
    def count(key):
        out: dict[str, int] = {}
        for r in rows:
            out[str(r[key])] = out.get(str(r[key]), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
    return {"checked_at_utc": checked_at, "parametres": params, "lignes": len(rows),
            "urls_distinctes": len({r["url_candidate"] for r in rows}),
            "communes": len({r["code_insee"] for r in rows}),
            "par_statut_acces": count("statut_acces"), "par_confiance": count("confiance"),
            "par_type_redirection": count("type_redirection"),
            "limitations": [
                "La confiance est une heuristique (nom de domaine, listes de plateformes) et non une preuve.",
                "Un contrôle unique : une erreur ponctuelle (timeout, 5xx, 429) ne prouve pas l'indisponibilité.",
                "Un statut 403/429 peut venir d'une protection anti-robot et non d'un site absent.",
                "Le fichier robots.txt n'est pas consulté à ce stade (une seule requête sur la page d'accueil) ;"
                " il le sera avant tout crawl (lot 3).",
                "Les contrôles s'exécutent depuis la connexion de l'utilisateur, à une date donnée."]}


def reclassify(paths: DeptPaths) -> int:
    """Réapplique les heuristiques aux contrôles déjà réalisés (aucune requête réseau)."""
    out_path = paths.url_checks
    if not out_path.exists():
        log.error("%s introuvable", out_path)
        return 1
    rows = csvio.read_rows(out_path)
    for r in rows:
        r["type_redirection"] = redirect_type(r["url_candidate"], r["final_url"])
        r["confiance"], r["motif_confiance"] = assess_confidence(
            r["nom_commune"], r["url_candidate"], r["final_url"] or None, r["statut_acces"],
            int(r["nb_communes_meme_url"] or 1), r["type_redirection"])
    write_rows(out_path, rows)
    summary = summarize(rows, datetime.now(timezone.utc).isoformat(), {"reclassify": True})
    paths.url_checks_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{paths.dept}] Reclassement terminé (sans réseau).")
    print("Confiance :", summary["par_confiance"])
    return 0


def _prepare(paths: DeptPaths, args) -> dict | None:
    if not paths.inventory_urls.exists():
        log.error("[%s] %s introuvable : lancez d'abord l'étape inventaire", paths.dept, paths.inventory_urls)
        return None
    all_inputs = load_input(paths.inventory_urls)
    inputs = all_inputs[: args.limit] if args.limit else all_inputs
    existing = load_previous(paths.url_checks)               # résultats déjà présents (pour --resume et pour --limit)
    previous = existing if args.resume else {}
    todo = {r["url_candidate"] for r in inputs
            if (r["code_insee"], r["url_candidate"]) not in previous
            or previous[(r["code_insee"], r["url_candidate"])]["statut_acces"] in RETRY_STATUTS}
    log.info("[%s] %d lignes, %d URL distinctes, %d à contrôler", paths.dept, len(inputs),
             len({r["url_candidate"] for r in inputs}), len(todo))
    return {"paths": paths, "inputs": inputs, "previous": previous, "existing": existing, "todo": todo}


def _finalize(prep: dict, results: dict[str, dict], args, checked_at: str) -> None:
    paths, inputs, previous = prep["paths"], prep["inputs"], prep["previous"]
    stamped = {u: {**r, "date_controle": checked_at} for u, r in results.items() if u in prep["todo"]}
    for r in inputs:                                          # résultats précédents conservés (--resume)
        key = (r["code_insee"], r["url_candidate"])
        if r["url_candidate"] not in stamped and key in previous:
            p = previous[key]
            stamped[r["url_candidate"]] = {k: p[k] for k in ("http_status", "statut_acces", "final_url",
                                           "nb_redirections", "content_type", "duree_ms", "erreur", "date_controle")}
    rows = build_rows(inputs, stamped, checked_at)
    if args.limit:                                            # essai partiel : ne détruit pas les contrôles existants
        keys = {(r["code_insee"], r["url_candidate"]) for r in rows}
        rows += [r for k, r in prep["existing"].items() if k not in keys]
    write_rows(paths.url_checks, rows)
    params = {k: getattr(args, k, None) for k in ("concurrency", "host_delay", "timeout", "max_redirects", "limit", "resume")}
    summary = summarize(rows, checked_at, params)
    paths.url_checks_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[{paths.dept}] Contrôle des URL terminé : {summary['lignes']} lignes, {summary['urls_distinctes']} URL distinctes.")
    print("  Accessibilité :", summary["par_statut_acces"])
    print("  Confiance     :", summary["par_confiance"])
    print("  Redirections  :", summary["par_type_redirection"])


def run_many(paths_list: list[DeptPaths], args) -> int:
    """Vérifie les URL de tous les départements dans UN SEUL pool (parallélisme global)."""
    if getattr(args, "reclassify", False):
        return max(reclassify(p) for p in paths_list)
    preps = [p for p in (_prepare(pl, args) for pl in paths_list) if p is not None]
    rc = 0 if len(preps) == len(paths_list) else 1
    todo_urls = sorted({u for p in preps for u in p["todo"]})
    ua = f"{UA_BASE} {args.contact}".strip()
    checked_at = datetime.now(timezone.utc).isoformat()

    def progress(done, total, url, res):
        if done % 25 == 0 or done == total:
            log.info("  %d/%d contrôlées (dernière : %s -> %s)", done, total, url, res["statut_acces"])

    try:
        results = asyncio.run(check_all(todo_urls, args.concurrency, args.host_delay, args.timeout,
                                        args.max_redirects, ua, on_progress=progress)) if todo_urls else {}
    except KeyboardInterrupt:
        log.error("Interrompu : relancez avec --resume (rien n'a été écrit pour cette exécution).")
        return 130
    for prep in preps:
        _finalize(prep, results, args, checked_at)
    return rc


def run(paths: DeptPaths, args) -> int:
    return run_many([paths], args)
