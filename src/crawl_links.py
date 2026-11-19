#!/usr/bin/env python3
"""Détecteur de liens vers https://vosquestions.ecologie-territoires.gouv.fr

Entrée  : reports/<dep>/inventory_urls.csv (+ sites_devines.csv si l'étape discover a trouvé des sites)
Sorties : reports/<dep>/links_found.csv   une ligne par lien trouvé (page, URL cible, zone…)
          reports/<dep>/crawl_sites.csv   une ligne par site : statut d'exploration et couverture
          reports/<dep>/crawl_summary.json

Principes :
  * parcours en LARGEUR, sans aucune priorité de contenu, et ARRÊT au premier lien trouvé sur un site ;
  * asyncio : une file de sites traitée par N tâches concurrentes ; plusieurs processus (un par cœur utile) ;
    analyse HTML avec selectolax (Lexbor) ;
  * robots.txt consulté pour chaque origine avant toute page (Disallow, Crawl-delay) ; délai minimal par site ;
    plafond de requêtes simultanées par domaine d'hébergement ; ralentissement automatique sur 429/503 ;
    taille de page, nombre de pages, profondeur et durée limités ; User-Agent identifiable ;
  * les résultats d'un département sont écrits dès que ses sites sont tous traités (et par points de sauvegarde).
"""
from __future__ import annotations

import asyncio
import heapq
import ipaddress
import itertools
import json
import logging
import math
import multiprocessing as mp
import os
import queue as queue_mod
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.lexbor import LexborHTMLParser

import csvio
import metrics as mt
import tuning
from layout import DeptPaths
from verify_urls import assess_confidence, classify_status, redirect_type, registrable

log = logging.getLogger("crawl_links")
TARGET_HOST = "vosquestions.ecologie-territoires.gouv.fr"
UA_TOKEN = "MairieLinkMonitor"
UA_BASE = f"{UA_TOKEN}/0.11 (detection de liens)"
MAX_BODY = 5_000_000
MAX_CRAWL_DELAY = 10.0
MAX_KNOWN_PAGES = 3
MAX_ALTERNATES = 3
SITEMAP_SEEDS = 15
SITEMAP_MAX_LOC = 2000
CHECKPOINT_EVERY = 100
METRICS: "mt.Metrics | None" = None     # collecteur du processus courant (None = mesures désactivées)
MAX_PER_PROCESS = 100      # au-delà, le coût CPU par page augmente et le débit baisse (mesuré)
HTML_TYPES = ("text/html", "application/xhtml+xml")
SKIP_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".zip", ".rar", ".7z", ".doc", ".docx",
            ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".mp3", ".mp4", ".avi", ".mov", ".css", ".js", ".xml",
            ".json", ".rss", ".ics", ".csv", ".txt", ".exe", ".apk"}
# Tous les niveaux sauf « institution_non_communale » : la vérification ne sert plus de filtre.
DEFAULT_INCLUDE = ("probable_officiel", "a_verifier", "plateforme_tierce_a_verifier", "intercommunal_ou_service_probable",
                   "tiers_probable", "devine_a_verifier")
RETRY_STATUTS = {"erreur_acces", "robots_indisponible"}
# erreur_dns : sous forte charge le serveur DNS de la box échoue par intermittence (mesuré : 111 sites lus le 25/10
# en erreur DNS le 28/10) ; une seconde tentative est donc faite comme pour les autres erreurs passagères.
TRANSIENT_MARKERS = ("timeout", "erreur_connexion", "http_5xx", "bloque_ou_limite", "erreur_dns")
DNS_ATTEMPTS = 3
PP_VILLE_RE = re.compile(r"^/ville/(\d+)-([a-z0-9-]+)-(\d{5})/?$")   # page commune PanneauPocket
PP_HOST = "app.panneaupocket.com"
JS_REDIRECT_RE = re.compile(r"""location(?:\.href)?\s*=\s*["']([^"']+)|location\.(?:replace|assign)\(\s*["']([^"']+)""")
META_REFRESH_RE = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.I)
CLICKABLE = ("lien", "iframe", "lien_attribut", "lien_script")     # types qui comptent comme « lien trouvé »
TARGET_RE = re.compile(r"(?<![\w.-])(?:https?:)?(?://)?" + re.escape(TARGET_HOST) + r"[^\s\"'<>()]*", re.I)
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "replytocom", "share", "print", "amp", "output"}
PAGER_PARAMS = {"page", "paged", "pg", "pagenum", "start", "offset", "debut", "date", "annee", "year", "mois", "month",
                "jour", "day", "cal", "calendar", "calendrier", "month_offset", "periode", "semaine", "week"}
PAGER_PATH_RE = re.compile(r"/(?:page|pages|p)/\d+/?$|/(?:19|20)\d\d/(?:0?[1-9]|1[0-2])(?:/\d{1,2})?/?$")
TRAP_CAP = 8        # au plus 8 pages par gabarit « pagination / calendrier » : au-delà ce sont des variantes sans intérêt
FALLBACK_PATHS = ("/plan-du-site", "/mentions-legales", "/contact", "/accessibilite", "/actualites", "/sitemap")
CHALLENGE_RE = re.compile(r"just a moment|attention required|checking your browser|captcha|access denied|acc[eè]s refus"
                          r"|are you a (?:robot|human)|v[ée]rification de s[ée]curit", re.I)
MENU_TAGS = {"nav", "header", "footer", "aside"}
MENU_RE = re.compile(r"(^|[\s_-])(menu|nav|navbar|navigation|header|footer|sidebar|topbar|bandeau|masthead)([\s_-]|$)", re.I)
TARGET_CODE_RE = re.compile(r"^/(\d{5}|2[AB]\d{3})(?:[-/]|$)", re.I)

LINK_FIELDS = ["code_insee", "nom_commune", "site_url", "page_url", "target_url", "href_origine", "texte_lien",
               "type_lien", "zone", "cible_commune", "confiance_site", "ssl_invalide", "date_controle"]
SITE_FIELDS = ["code_insee", "nom_commune", "site_url", "confiance_lot2", "url_utilisee", "confiance_finale",
               "statut_crawl", "motif", "ssl_invalide", "pages_visitees", "requetes_http", "pages_en_erreur",
               "pages_interdites_robots", "liens_trouves", "profondeur_max_atteinte", "limite_pages_atteinte",
               "limite_profondeur_atteinte", "pages_tronquees", "tentatives", "processus", "duree_s", "erreur",
               "pages_plateformes", "accueil_diag", "variantes_ignorees", "date_controle"]


# ----------------------------------------------------------------- fonctions pures (testées)
def norm_host(host: str | None) -> str:
    return (host or "").lower().rstrip(".").removeprefix("www.")


def is_target_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return p.scheme.lower() in ("http", "https") and norm_host(p.hostname) == TARGET_HOST
    except ValueError:
        return False


def normalize_internal(url: str) -> str:
    """URL canonique : sans ancre ni paramètres de suivi (utm_*, fbclid…), qui donnent la même page sous cent adresses."""
    p = urlsplit(url)
    query = p.query
    if query:
        kept = [kv for kv in query.split("&") if (kv.split("=", 1)[0].lower() not in TRACKING_PARAMS
                                                 and not kv.lower().startswith("utm_"))]
        query = "&".join(kept)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", query, ""))


def page_id(url: str) -> tuple[str, str, str]:
    """Identité d'une page pour comparer deux adresses de la même page (/fr et /fr/, /index.html, www)."""
    p = urlsplit(url)
    return norm_host(p.hostname), re.sub(r"/index\.(?:html?|php)$", "", p.path).rstrip("/") or "/", p.query


def trap_key(url: str) -> str:
    """Gabarit d'une URL de pagination ou de calendrier ('' sinon). Exemple : /agenda?date=2026-03 -> '/agenda?date'.
    Sert à ne lire que TRAP_CAP variantes d'un même gabarit, sans toucher aux pages de contenu."""
    p = urlsplit(url)
    names = sorted({kv.split("=", 1)[0].lower() for kv in p.query.split("&") if kv})
    if any(n in PAGER_PARAMS for n in names):
        return f"{p.path}?{'&'.join(names)}"
    if PAGER_PATH_RE.search(p.path):
        return re.sub(r"\d+", "#", p.path)
    return ""


def crawlable(url: str, site_host: str) -> bool:
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if p.scheme.lower() not in ("http", "https") or norm_host(p.hostname) != site_host or len(url) > 300:
        return False
    return Path(p.path).suffix.lower() not in SKIP_EXT


def link_zone(node) -> str:
    """'menu_ou_bandeau' si le lien est dans nav/header/footer/aside ou un bloc nommé menu/nav/header/footer…
    (donc présent sur toutes les pages d'un site) ; sinon 'contenu'."""
    p = node.parent
    while p is not None:
        if p.tag in MENU_TAGS:
            return "menu_ou_bandeau"
        attrs = p.attributes
        if MENU_RE.search(f"{attrs.get('class') or ''} {attrs.get('id') or ''}"):
            return "menu_ou_bandeau"
        p = p.parent
    return "contenu"


def target_scope(target_url: str, code_insee: str) -> str:
    """Le lien mène-t-il à la page de CETTE commune sur le portail ? 'oui' (code INSEE dans l'URL cible),
    'autre_commune' (un autre code INSEE) ou 'generique' (pas de code)."""
    m = TARGET_CODE_RE.match(urlsplit(target_url).path)
    if not m:
        return "generique"
    return "oui" if m.group(1).upper() == str(code_insee).upper() else "autre_commune"


@dataclass
class PageData:
    links: list[dict] = field(default_factory=list)
    internal: list[str] = field(default_factory=list)
    platform_pages: list[str] = field(default_factory=list)   # pages PanneauPocket liées depuis la page
    invalid_hrefs: int = 0
    n_anchors: int = 0          # nombre de balises <a href> (diagnostic de l'accueil)
    n_scripts: int = 0
    text_len: int = 0           # longueur du texte visible
    cause: str = ""             # accueil quasi vide : raison probable (voir weak_home_cause)


def raw_target_hits(tree, html: str) -> list[tuple[str, str, str]]:
    """Mentions du portail hors balises <a>/<iframe> : onclick, data-*, widget <script>, texte brut. Retourne
    [(type_lien, href_origine, URL absolue)]. Les <link>/<meta> (préconnexion, canonical) ne sont pas des liens."""
    hits: list[tuple[str, str, str]] = []

    def add(kind: str, raw: str) -> None:
        m = TARGET_RE.search(unquote(raw.replace("\\/", "/")))        # \/ (JSON) et %3A%2F%2F (lien de redirection)
        if not m:
            return
        url = m.group(0)
        url = url if url.lower().startswith("http") else "https://" + url.lstrip(":/")
        if is_target_url(url):
            hits.append((kind, raw.strip()[:200], url))
    for node in tree.css("*"):
        if node.tag in ("link", "meta", "base"):
            continue
        for value in node.attributes.values():
            if value and TARGET_HOST in value.lower():
                add("lien_script" if node.tag == "script" else "lien_attribut", value)
    for node in tree.css("script"):
        text = node.text() or ""
        if TARGET_HOST in text.lower():
            add("lien_script", text[max(0, text.lower().find(TARGET_HOST) - 80):][:300])
    if not hits:
        tree.strip_tags(["script", "style"])
        text = tree.body.text(separator=" ") if tree.body is not None else ""
        if TARGET_HOST in text.lower():
            i = text.lower().find(TARGET_HOST)
            add("texte", text[max(0, i - 40):i + 160])
    return hits


def weak_home_cause(data: PageData, html: str) -> str:
    """Pourquoi une page d'accueil ne donne (presque) aucun lien interne : sert au compte rendu et à décider la suite."""
    if CHALLENGE_RE.search(html[:30000]):
        return "page de protection anti-robot probable"
    if data.n_scripts >= 3 and data.text_len < 400:
        return "page construite par JavaScript probable"
    if data.n_anchors == 0:
        return "page sans aucun lien"
    if data.n_anchors >= 3:
        return "liens vers d'autres domaines uniquement"
    return "page d'accueil très courte"


def parse_page(html: str, page_url: str, site_host: str, diagnose: bool = False) -> PageData:
    """Analyse HTML avec selectolax (Lexbor) : liens vers le portail cible + liens internes à suivre."""
    data = PageData()
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # noqa: BLE001 - document inanalysable : compté, jamais fatal
        data.invalid_hrefs += 1
        return data
    base = page_url
    b = tree.css_first("base[href]")
    if b is not None:
        try:
            base = urljoin(page_url, (b.attributes.get("href") or "").strip())
        except ValueError:
            pass
    seen_targets: set[tuple[str, str]] = set()
    seen_internal: set[str] = set()

    def add_target(absolute: str, href: str, text: str, kind: str, zone: str) -> None:
        if (absolute, kind) not in seen_targets:
            seen_targets.add((absolute, kind))
            data.links.append({"target_url": absolute, "href_origine": href, "texte_lien": text[:120],
                               "type_lien": kind, "zone": zone})

    for node in tree.css("a[href], area[href]"):
        data.n_anchors += 1
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "sms:")):
            continue
        try:
            absolute = urljoin(base, href)
        except ValueError:
            data.invalid_hrefs += 1
            continue
        if is_target_url(absolute):
            add_target(absolute, href, node.text(separator=" ", strip=True), "lien", link_zone(node))
        elif crawlable(absolute, site_host):
            norm = normalize_internal(absolute)
            if norm not in seen_internal:
                seen_internal.add(norm)
                data.internal.append(norm)
        else:
            pp = panneaupocket_page(absolute)
            if pp and pp not in data.platform_pages:
                data.platform_pages.append(pp)
    for node in tree.css("iframe[src], embed[src]"):
        src = (node.attributes.get("src") or "").strip()
        try:
            absolute = urljoin(base, src)
        except ValueError:
            data.invalid_hrefs += 1
            continue
        if is_target_url(absolute):
            add_target(absolute, src, "", "iframe", link_zone(node))
    # Pages internes indiquées hors des liens <a> : redirection automatique (meta refresh), cadres, redirection
    # JavaScript simple. Sans cela un accueil qui redirige est noté « lu en entier » avec une seule page.
    extra = [(n.attributes.get("src") or "").strip() for n in tree.css("frame[src], iframe[src]")]
    for n in tree.css("meta[http-equiv]"):
        if (n.attributes.get("http-equiv") or "").lower() == "refresh":
            m = META_REFRESH_RE.search(n.attributes.get("content") or "")
            if m:
                extra.append(m.group(1).strip())
    for n in tree.css("script"):
        extra += [a or b for a, b in JS_REDIRECT_RE.findall(n.text() or "")[:3]]
    for src in extra:
        try:
            absolute = urljoin(base, src)
        except ValueError:
            continue
        if crawlable(absolute, site_host) and normalize_internal(absolute) not in seen_internal:
            seen_internal.add(normalize_internal(absolute))
            data.internal.append(normalize_internal(absolute))
    if not data.links and TARGET_HOST in html.lower():          # lien non balisé : onclick, data-*, widget, texte brut
        for kind, origin, url in raw_target_hits(tree, html)[:3]:
            add_target(url, origin, "", kind, "contenu")
    if diagnose and len(data.internal) < 2:
        data.n_scripts = len(tree.css("script"))
        data.text_len = len((tree.body.text(separator=" ", strip=True) if tree.body is not None else ""))
        data.cause = weak_home_cause(data, html)
    return data


def panneaupocket_page(url: str) -> str:
    """URL canonique d'une page commune PanneauPocket (sans ?panneau=…), ou '' si ce n'en est pas une."""
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    if (p.hostname or "").lower() != PP_HOST or not PP_VILLE_RE.match(p.path):
        return ""
    return f"https://{PP_HOST}{p.path.rstrip('/')}"


def select_sites(rows: list[dict], include=DEFAULT_INCLUDE) -> tuple[list[dict], list[dict]]:
    """rows : {code_insee, nom_commune, url_candidate, confiance}. Retourne (sites à explorer, sites écartés + motif).
    La vérification préalable n'est plus un filtre : la première requête de l'exploration tranche."""
    todo, skipped = [], []
    for r in rows:
        base = {"code_insee": r["code_insee"], "nom_commune": r["nom_commune"], "confiance_lot2": r["confiance"],
                "start_url": r["url_candidate"]}
        if r["confiance"] in include:
            todo.append({**base, "ssl_invalide": False})
        else:
            skipped.append({**base, "statut_crawl": "non_explore", "motif": f"non exploré : confiance={r['confiance']}"})
    return todo, skipped


def alternate_starts(url: str) -> list[str]:
    """URL de repli si la page d'accueil échoue : racine du site (chemin périmé), avec/sans www, http/https.
    Au plus MAX_ALTERNATES, sans doublon."""
    p = urlsplit(url)
    host = p.hostname or ""
    if not host:
        return []
    port = f":{p.port}" if p.port else ""
    toggled = (host[4:] if host.startswith("www.") else f"www.{host}") + port
    other = "https" if p.scheme == "http" else "http"
    cands = []
    if p.path not in ("", "/") or p.query:
        cands.append(f"{p.scheme}://{p.netloc}/")
    if p.scheme == "http":                       # http : la version https est la plus probable
        cands += [f"https://{p.netloc}/", f"https://{toggled}/", f"http://{toggled}/"]
    else:
        cands += [f"https://{toggled}/", f"{other}://{p.netloc}/", f"{other}://{toggled}/"]
    out: list[str] = []
    for c in cands:
        if normalize_internal(c) != normalize_internal(url) and c not in out:
            out.append(c)
    return out[:MAX_ALTERNATES]


def shard_of(url: str, workers: int) -> int:
    """Même domaine d'hébergement -> même processus (les plafonds par domaine restent valables). Déterministe."""
    host = norm_host(urlsplit(url).hostname)
    return zlib.crc32(registrable(host).encode()) % workers


def sitemap_locs(xml: str) -> list[str]:
    return [m.strip() for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)][:SITEMAP_MAX_LOC]


# ----------------------------------------------------------------- réseau
class Throttle:
    """Délai minimal entre deux requêtes vers un même site (surchargeable : Crawl-delay, ralentissement 429/503)."""

    def __init__(self, delay: float):
        self.delay, self.per_host, self._locks, self._last = delay, {}, {}, {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            d = self.per_host.get(host, self.delay)
            gap = d - (time.monotonic() - self._last.get(host, -1e9))
            if gap > 0:
                await asyncio.sleep(gap)
            self._last[host] = time.monotonic()

    def slow_down(self, host: str) -> None:
        self.per_host[host] = min(max(self.per_host.get(host, self.delay), 0.5) * 2, MAX_CRAWL_DELAY)


@dataclass
class Fetched:
    status: int | None = None
    final_url: str = ""
    html: str = ""
    content_type: str = ""
    wait_s: float = 0.0     # temps passé à attendre le délai de politesse (exclu de la latence mesurée)
    nbytes: int = 0
    truncated: bool = False
    kind: str = ""          # "", "non_html", "redirection_externe", ou un statut de classify_status
    error: str = ""


async def _fetch(client: httpx.AsyncClient, url: str, site_host: str, throttle: Throttle, max_redirects: int = 5,
                 want_body: bool = True, html_only: bool = True, timeout: float | None = None,
                 follow_external: bool = False) -> Fetched:
    cur = url
    kw = {"timeout": timeout} if timeout else {}
    waited, nbytes = 0.0, 0
    m = METRICS

    def done(out: Fetched) -> Fetched:
        out.wait_s, out.nbytes = waited, nbytes
        return out
    try:
        for _ in range(max_redirects + 1):
            t0 = time.monotonic()
            if m is not None:
                m.waiting += 1
            try:
                await throttle.wait(norm_host(urlsplit(cur).hostname))
            finally:
                if m is not None:
                    m.waiting -= 1
            waited += time.monotonic() - t0
            if m is not None:
                m.inflight += 1
            try:
                async with client.stream("GET", cur, **kw) as resp:
                    if resp.status_code in (429, 503):
                        throttle.slow_down(site_host)                 # le serveur demande de ralentir : on obéit
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        nxt = urljoin(cur, resp.headers["location"])
                        if norm_host(urlsplit(nxt).hostname) != site_host and not follow_external:
                            return done(Fetched(resp.status_code, nxt, kind="redirection_externe"))
                        cur = nxt
                        continue
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    out = Fetched(resp.status_code, str(resp.url), content_type=ctype)
                    out.kind = classify_status(resp.status_code, None)
                    if want_body and 200 <= resp.status_code < 300:
                        if html_only and ctype and ctype not in HTML_TYPES:
                            out.kind = "non_html"
                            return done(out)
                        chunks, size = [], 0
                        async for chunk in resp.aiter_bytes():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size >= MAX_BODY:
                                break
                        wire = resp.num_bytes_downloaded or size      # octets reçus sur le réseau (compressés) ; `or size` : réponses simulées
                        nbytes += wire
                        if m is not None:
                            m.bytes += wire
                        out.truncated = size >= MAX_BODY
                        out.html = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                    return done(out)
            finally:
                if m is not None:
                    m.inflight -= 1
        return done(Fetched(None, cur, kind="trop_de_redirections"))
    except Exception as exc:  # noqa: BLE001 - classée puis enregistrée
        return done(Fetched(None, cur, kind=classify_status(None, exc), error=f"{type(exc).__name__}: {exc}"[:200]))


async def fetch(*args, **kwargs) -> Fetched:
    """GET avec suivi des redirections ; enregistre l'issue et la LATENCE RÉSEAU (hors attente de politesse)."""
    m = METRICS
    if m is None:
        return await _fetch(*args, **kwargs)
    t0 = time.monotonic()
    res = await _fetch(*args, **kwargs)
    m.record(res.kind, res.status, max(0.0, time.monotonic() - t0 - res.wait_s))
    return res


async def load_robots(client, robots_origin_url: str, site_host: str, throttle: Throttle, timeout: float | None = None):
    """Retourne (parser|None, état, nature de l'échec, statut HTTP). état : ok | absent | indisponible."""
    p = urlsplit(robots_origin_url)
    res = await fetch(client, f"{p.scheme}://{p.netloc}/robots.txt", site_host, throttle, html_only=False, timeout=timeout,
                      follow_external=True)               # RFC 9309 : au moins 5 redirections, même vers un autre domaine
    if res.status is not None and 200 <= res.status < 300:
        rp = RobotFileParser()
        rp.parse(res.html.splitlines())
        return rp, "ok", "", res.status
    if res.status in (401, 403):
        rp = RobotFileParser()
        rp.parse(["User-agent: *", "Disallow: /"])
        return rp, "ok", "", res.status
    if res.status is not None and 400 <= res.status < 500:
        return None, "absent", "", res.status          # pas de robots.txt : exploration autorisée
    if res.kind == "trop_de_redirections" or (res.status is not None and 300 <= res.status < 400):
        return None, "absent", "", res.status          # RFC 9309 : redirection sans issue = robots.txt indisponible (4xx)
    return None, "indisponible", res.kind, res.status  # 5xx / erreur réseau : prudence


@dataclass
class SiteResult:
    row: dict
    links: list[dict]
    dept: str = ""


async def crawl_site(site: dict, clients: tuple, throttle: Throttle, opts: dict, attempt: int = 1) -> SiteResult:
    """Explore un site en largeur et S'ARRÊTE au premier lien trouvé."""
    secure, insecure = clients
    client = insecure if (site["ssl_invalide"] and insecure is not None) else secure
    started = time.monotonic()
    start = site["start_url"]
    site_host = norm_host(urlsplit(start).hostname)
    now = datetime.now(timezone.utc).isoformat()
    req_timeout = opts["timeout"] * (1.5 if attempt > 1 else 1.0)
    if attempt > 1:                                            # deuxième tentative : plus lente
        throttle.per_host[site_host] = max(throttle.per_host.get(site_host, throttle.delay), throttle.delay * 2)
    row = {"code_insee": site["code_insee"], "nom_commune": site["nom_commune"], "site_url": start,
           "confiance_lot2": site["confiance_lot2"], "url_utilisee": "", "confiance_finale": "", "statut_crawl": "",
           "motif": "", "ssl_invalide": int(site["ssl_invalide"]), "pages_visitees": 0, "requetes_http": 0,
           "pages_en_erreur": 0, "pages_interdites_robots": 0, "liens_trouves": 0, "profondeur_max_atteinte": 0,
           "limite_pages_atteinte": 0, "limite_profondeur_atteinte": 0, "pages_tronquees": 0, "tentatives": attempt,
           "processus": os.getpid(), "duree_s": 0, "erreur": "", "pages_plateformes": "", "accueil_diag": "",
           "variantes_ignorees": 0, "date_controle": now}
    links: list[dict] = []
    platform_pages: list[str] = []
    robots_cache: dict[str, tuple] = {}

    async def get(url, **kw):
        row["requetes_http"] += 1
        return await fetch(client, url, site_host, throttle, timeout=req_timeout, **kw)

    def use_insecure() -> bool:
        """Certificat invalide : on bascule (une seule fois) sur le client sans vérification, et on le signale."""
        nonlocal client
        if insecure is None or client is insecure:
            return False
        client = insecure
        site["ssl_invalide"] = True
        row["ssl_invalide"] = 1
        return True

    async def robots_for(url: str) -> tuple:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in robots_cache:
            for _ in range(2):
                row["requetes_http"] += 1
                result = await load_robots(client, origin + "/", site_host, throttle, req_timeout)
                if result[1] == "indisponible" and result[2] == "erreur_ssl" and use_insecure():
                    continue
                break
            rp, state = result[0], result[1]
            if rp is not None and state == "ok":
                cd = rp.crawl_delay(UA_TOKEN) or rp.crawl_delay("*")
                if cd:
                    throttle.per_host[site_host] = min(max(float(cd), throttle.delay), MAX_CRAWL_DELAY)
            robots_cache[origin] = result
        return robots_cache[origin]

    async def body():
        nonlocal site_host
        moved = False
        # Parcours en LARGEUR, sans priorité de contenu. Clé : (niveau, ordre d'arrivée, url, profondeur) ;
        # niveau -1 = page où le lien avait déjà été vu lors d'un précédent passage.
        heap: list[tuple[int, int, str, int]] = []
        tick = itertools.count()
        seen: set[str] = set()

        templates: dict[str, int] = {}
        probes: set[str] = set()                       # pages devinées : un échec n'est pas une erreur du site
        weak_done, sitemap_tried, accessible, mention = False, False, 0, ""

        def push(url: str, depth: int, known: bool = False) -> None:
            if url in seen:
                return
            seen.add(url)
            key = "" if known else trap_key(url)
            if key:
                templates[key] = templates.get(key, 0) + 1
                if templates[key] > TRAP_CAP:           # pagination / calendrier : variantes sans intérêt
                    row["variantes_ignorees"] += 1
                    return
            heapq.heappush(heap, (-1 if known else depth, next(tick), url, depth))

        push(normalize_internal(start), 0)
        alternates = alternate_starts(start)
        for kp in (site.get("known_pages") or [])[:MAX_KNOWN_PAGES]:
            if crawlable(kp, site_host):
                push(normalize_internal(kp), 1, known=True)

        def next_alternate() -> bool:
            while alternates:
                alt = normalize_internal(alternates.pop(0))
                if alt not in seen:
                    push(alt, 0)
                    return True
            return False

        errors, first_error, sitemap_done = 0, "", not opts.get("use_sitemap", False)
        while heap:
            if row["pages_visitees"] >= opts["max_pages"]:
                row["limite_pages_atteinte"] = 1
                break
            level, _, url, depth = heapq.heappop(heap)
            rp, rstate, rkind, rstatus = await robots_for(url)
            if rstate == "indisponible":
                if depth == 0:
                    first_error = first_error or f"{url} -> robots {rkind} {rstatus or ''}".strip()
                    if next_alternate():
                        continue
                    if rstatus is not None:
                        row.update(statut_crawl="robots_indisponible", erreur=first_error[:200],
                                   motif=f"robots.txt injoignable (HTTP {rstatus}) : pas d'exploration")
                    else:
                        errors += 1
                        row.update(statut_crawl="erreur_acces", motif=f"page d'accueil : {rkind}",
                                   erreur=first_error[:200], pages_en_erreur=errors)
                    return
                continue
            if rp is not None and not rp.can_fetch(UA_TOKEN, url):
                if url in probes:
                    continue
                row["pages_interdites_robots"] += 1
                if depth == 0:
                    if next_alternate():
                        continue
                    row.update(statut_crawl="robots_interdit", motif="robots.txt interdit la page d'accueil")
                    return
                continue
            res = await get(url)
            row["pages_visitees"] += 1
            if res.truncated:
                row["pages_tronquees"] += 1
            row["profondeur_max_atteinte"] = max(row["profondeur_max_atteinte"], depth)
            if res.kind == "accessible":
                if depth == 0 and not row["url_utilisee"]:
                    row["url_utilisee"] = res.final_url or url
                accessible += 1
                data = parse_page(res.html, res.final_url or url, site_host, diagnose=depth == 0 and not weak_done)
                platform_pages.extend(u for u in data.platform_pages if u not in platform_pages)
                for l in data.links:
                    if l["type_lien"] not in CLICKABLE:       # URL en texte brut, sans lien : signalée, non comptée
                        mention = mention or res.final_url or url
                        continue
                    links.append({"code_insee": site["code_insee"], "nom_commune": site["nom_commune"],
                                  "site_url": start, "page_url": res.final_url or url, **l,
                                  "cible_commune": target_scope(l["target_url"], site["code_insee"]),
                                  "confiance_site": site["confiance_lot2"],
                                  "ssl_invalide": int(site["ssl_invalide"]), "date_controle": now})
                if links:                                              # ARRÊT au premier lien trouvé (cliquable)
                    row.update(statut_crawl="lien_trouve", erreur=first_error[:200],
                               motif="lien confirmé sur une page déjà connue" if level == -1
                               else "arrêt au premier lien trouvé")
                    return
                if depth < opts["max_depth"]:
                    for nxt in data.internal:
                        push(nxt, depth + 1)
                elif any(n not in seen for n in data.internal):
                    row["limite_profondeur_atteinte"] = 1
                if depth == 0 and not sitemap_done and opts["max_depth"] >= 1:
                    sitemap_done = sitemap_tried = True
                    for nxt in await sitemap_candidates(get, start, rp, site_host, SITEMAP_SEEDS):
                        push(nxt, 1)
                here = normalize_internal(res.final_url or url)
                if depth == 0 and not weak_done and opts["max_depth"] >= 1 and all(page_id(u) == page_id(here) for u in data.internal):
                    # Accueil sans AUCUN autre lien interne : page JavaScript, protection anti-robot ou site d'une
                    # page. Plan B : plan du site, puis pages usuelles (sondes), au lieu de conclure « lu en entier ».
                    weak_done = True
                    row["accueil_diag"] = (f"{data.cause} ; {len(res.html)} o ; {data.n_anchors} lien(s) ; "
                                           f"{data.n_scripts} script(s) ; {data.text_len} car. de texte")
                    if not sitemap_tried:
                        sitemap_tried = True
                        for nxt in await sitemap_candidates(get, start, rp, site_host, SITEMAP_SEEDS):
                            push(nxt, 1)
                    origin = "{0.scheme}://{0.netloc}".format(urlsplit(res.final_url or url))
                    for path in FALLBACK_PATHS:
                        probe = normalize_internal(origin + path)
                        probes.add(probe)
                        push(probe, 1)
            elif res.kind == "non_html":
                if url in probes:
                    row["pages_visitees"] -= 1
            elif url in probes:                                        # sonde sans réponse (404…) : sans conséquence
                row["pages_visitees"] -= 1
            else:
                first_error = first_error or f"{url} -> {res.kind} {res.status or ''} {res.error}".strip()
                if depth == 0:
                    last_kind = res.kind
                    if res.kind == "redirection_externe" and not moved and res.final_url:
                        moved = True                                   # site déménagé (ex. beauvais.fr -> beauvaisis.fr)
                        new = normalize_internal(res.final_url)
                        site_host = norm_host(urlsplit(new).hostname)
                        first_error = f"site déplacé : {start} -> {new}"
                        alternates[:] = alternate_starts(new)
                        push(new, 0)
                        continue
                    if res.kind == "erreur_ssl" and use_insecure():    # certificat invalide : nouvel essai sans vérification
                        seen.discard(url)
                        push(url, 0)
                        continue
                    if next_alternate():                               # repli : racine, www, http/https
                        continue
                    errors += 1
                    row.update(statut_crawl="erreur_acces", motif=f"page d'accueil : {last_kind}",
                               erreur=first_error[:200], pages_en_erreur=errors)
                    return
                errors += 1
        row["pages_en_erreur"] = errors
        if heap and not row["limite_pages_atteinte"]:
            row["limite_pages_atteinte"] = 1
        weak = bool(row["accueil_diag"]) and accessible <= 3          # accueil vide et rien d'autre de lisible
        partial = (row["limite_pages_atteinte"] or row["limite_profondeur_atteinte"] or errors
                   or row["pages_tronquees"] or weak)
        motifs = [m for m, c in ((f"accueil sans lien interne : {row['accueil_diag'].split(' ;')[0]}", weak),
                                 (f"URL du portail en texte brut sans lien sur {mention}", mention),
                                 ("limite de pages", row["limite_pages_atteinte"]),
                                 ("limite de profondeur", row["limite_profondeur_atteinte"]),
                                 (f"{errors} page(s) en erreur", errors),
                                 (f"{row['pages_tronquees']} page(s) tronquée(s) (> {MAX_BODY // 1_000_000} Mo)",
                                  row["pages_tronquees"]),
                                 (f"{row['pages_interdites_robots']} page(s) interdites par robots.txt",
                                  row["pages_interdites_robots"])) if c]
        row.update(statut_crawl="explore_partiel" if partial else "explore_complet",
                   motif=", ".join(motifs) or "toutes les pages atteignables dans les limites ont été lues",
                   erreur=first_error[:200])

    try:
        await asyncio.wait_for(body(), timeout=opts["site_timeout"])
    except asyncio.TimeoutError:
        row.update(statut_crawl="explore_partiel", motif="durée maximale par site atteinte")
    row["liens_trouves"] = len(links)
    row["pages_plateformes"] = " | ".join(platform_pages[:5])
    row["duree_s"] = round(time.monotonic() - started, 1)
    return SiteResult(row, links, site.get("_dept", ""))


async def sitemap_candidates(get, start_url: str, robots, site_host: str, limit: int = SITEMAP_SEEDS) -> list[str]:
    """Premières pages du sitemap (dans l'ordre du fichier, sans notation), au plus 2 requêtes ; soumises à robots.txt."""
    p = urlsplit(start_url)
    declared = list(robots.site_maps() or []) if robots is not None and hasattr(robots, "site_maps") else []
    urls = [u for u in declared if norm_host(urlsplit(u).hostname) == site_host][:1] or [f"{p.scheme}://{p.netloc}/sitemap.xml"]
    locs: list[str] = []
    todo = list(urls)
    fetched = 0
    while todo and fetched < 2:
        u = todo.pop(0)
        if u.endswith(".gz"):
            continue
        res = await get(u, html_only=False)
        fetched += 1
        if res.status is None or not 200 <= res.status < 300 or not res.html:
            continue
        found = sitemap_locs(res.html)
        if "<sitemapindex" in res.html[:2000].lower():
            todo.extend([x for x in found if norm_host(urlsplit(x).hostname) == site_host][:1])
        else:
            locs.extend(found)
    out = [x for x in dict.fromkeys(normalize_internal(l) for l in locs if crawlable(l, site_host))
           if robots is None or robots.can_fetch(UA_TOKEN, x)]
    return out[:limit]


async def resolve(host: str, timeout: float = 5.0) -> str:
    """'' si le nom se résout, sinon le motif de l'échec (code d'erreur Windows compris : 11001 = nom inexistant,
    11002 = échec temporaire du serveur DNS)."""
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout)
        return ""
    except asyncio.TimeoutError:
        return "délai de résolution dépassé"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"[:150]


async def prefetch_dns(url: str, sem: asyncio.Semaphore, pause: float) -> str:
    """Résout le nom du site avant de l'explorer, avec au plus N résolutions simultanées par processus et des
    nouvelles tentatives espacées. Le résultat est mis en cache par le système : la connexion qui suit ne relance pas
    une résolution au moment où des centaines d'autres sont en cours. Retourne '' ou le dernier motif d'échec."""
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    err = ""
    for attempt in range(DNS_ATTEMPTS):
        async with sem:
            err = await resolve(host)
        if not err:
            return ""
        if attempt < DNS_ATTEMPTS - 1:
            await asyncio.sleep(pause * (attempt + 1))
    return err


def legacy_ssl_context() -> ssl.SSLContext:
    """Contexte pour les sites à certificat invalide (signalés ssl_invalide), tolérant aussi aux vieux serveurs TLS
    (clé Diffie-Hellman faible, renégociation non sécurisée) que OpenSSL 3 refuse par défaut."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    except (ssl.SSLError, ValueError):          # build OpenSSL sans ces réglages : contexte standard sans vérification
        pass
    return ctx


def tune_runtime(concurrency: int) -> None:
    """La résolution DNS passe par des threads : on relève leur plafond (40 par défaut) pour ne pas brider
    des centaines de sites explorés en parallèle."""
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = max(40, min(1000, concurrency * 2))
    except Exception:  # noqa: BLE001 - optimisation facultative
        pass


async def crawl_all(sites: list[dict], opts: dict, transport=None, on_done=None, on_result=None,
                    on_metrics=None) -> list[SiteResult]:
    """File de sites traitée par `concurrency` tâches asyncio. Plafond de sites simultanés par domaine d'hébergement."""
    tune_runtime(opts["concurrency"])
    ua = f"{UA_BASE} {opts.get('contact', '')}".strip()
    limits = httpx.Limits(max_connections=opts["concurrency"] * 2 + 10, max_keepalive_connections=opts["concurrency"])
    kw = dict(follow_redirects=False, timeout=opts["timeout"], transport=transport, limits=limits,
              headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
    throttle = Throttle(opts["host_delay"])
    todo: asyncio.Queue = asyncio.Queue()
    for s in sites:
        todo.put_nowait(s)
    domain_sems: dict[str, asyncio.Semaphore] = {}
    dns_sem = asyncio.Semaphore(max(1, opts.get("dns_simultanees", 16)))
    use_dns_prefetch = transport is None and opts.get("dns_prefetch", True)
    results: list[SiteResult] = []
    secure = httpx.AsyncClient(verify=True, **kw)
    insecure = httpx.AsyncClient(verify=legacy_ssl_context(), **kw) if opts.get("insecure_ssl", True) else None
    global METRICS
    metrics = mt.Metrics(os.getpid(), opts["concurrency"]) if on_metrics else None
    METRICS = metrics

    async def lag_probe() -> None:                       # retard de la boucle asyncio = indicateur de saturation
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.2)
            metrics.probe(max(0.0, (time.monotonic() - t - 0.2) * 1000))

    async def sampler() -> None:
        while True:
            await asyncio.sleep(opts.get("metrics_interval", 10.0))
            on_metrics(metrics.snapshot())

    async def worker() -> None:
        while True:
            try:
                site = todo.get_nowait()
            except asyncio.QueueEmpty:
                if metrics is not None:
                    metrics.queue_len = 0
                return
            if metrics is not None:
                metrics.queue_len = todo.qsize()
                metrics.active_sites += 1
            dom = registrable(norm_host(urlsplit(site["start_url"]).hostname))
            sem = domain_sems.setdefault(dom, asyncio.Semaphore(opts.get("max_par_domaine", 4)))
            pause = opts.get("dns_retry_pause", 2.0)
            dns_err = await prefetch_dns(site["start_url"], dns_sem, pause) if use_dns_prefetch else ""
            async with sem:
                res = await crawl_site(site, (secure, insecure), throttle, opts)
                if opts.get("retry_failed", True) and res.row["statut_crawl"] in RETRY_STATUTS and \
                        any(m in res.row["motif"] for m in TRANSIENT_MARKERS + ("HTTP 5", "HTTP 429")):
                    await asyncio.sleep(opts.get("retry_pause", 3.0))     # erreur passagère : 2e tentative, plus lente
                    if use_dns_prefetch and "erreur_dns" in res.row["motif"]:
                        dns_err = await prefetch_dns(site["start_url"], dns_sem, pause * 2)
                    res = await crawl_site(site, (secure, insecure), throttle, opts, attempt=2)
            if dns_err and "erreur_dns" in res.row["motif"]:
                res.row["erreur"] = f"DNS : {dns_err}"[:200]
            results.append(res)
            if metrics is not None:
                metrics.active_sites -= 1
                metrics.sites_done += 1
            if on_result:
                on_result(res)
            if on_done:
                on_done(len(results), len(sites), res.row)

    background = [asyncio.ensure_future(f()) for f in (lag_probe, sampler)] if metrics is not None else []
    try:
        await asyncio.gather(*(worker() for _ in range(max(1, min(opts["concurrency"], len(sites))))))
    finally:
        for t in background:
            t.cancel()
        if metrics is not None:
            on_metrics(metrics.snapshot())
            METRICS = None
        await secure.aclose()
        if insecure is not None:
            await insecure.aclose()
    return results


def _worker_main(shard: list[dict], opts: dict, out) -> None:
    """Processus enfant : sa propre boucle asyncio ; chaque résultat est renvoyé dès qu'il est prêt."""
    try:
        asyncio.run(crawl_all(shard, opts, on_result=lambda r: out.put(("result", r)),
                              on_metrics=(lambda m: out.put(("metrics", m))) if opts.get("metrics_enabled") else None))
    except BaseException as exc:  # noqa: BLE001
        out.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        out.put(("done", None))


def crawl_sites(sites: list[dict], opts: dict, on_result, workers: int = 1, on_progress=None, on_metrics=None) -> None:
    """Explore `sites` ; `on_result(SiteResult)` est appelé dans le processus principal pour chaque site terminé."""
    workers = max(1, min(workers, len(sites) // 20 or 1))          # inutile de multiplier les processus pour peu de sites
    cap = opts.get("max_per_process", MAX_PER_PROCESS)
    per_process = max(1, min(math.ceil(opts["concurrency"] / workers), cap))
    opts = {**opts, "concurrency": per_process, "metrics_enabled": on_metrics is not None,
            "dns_simultanees": max(1, math.ceil(opts.get("dns_simultanees", 48) / workers))}
    log.info("Exploration : %d site(s), %d processus × %d tâches asyncio", len(sites), workers, per_process)
    if workers == 1:
        asyncio.run(crawl_all(sites, opts, on_result=on_result, on_done=on_progress, on_metrics=on_metrics))
        return
    ctx = mp.get_context("spawn")                                    # même comportement sous Windows et Linux
    out = ctx.Queue()
    shards: list[list[dict]] = [[] for _ in range(workers)]
    for s in sites:
        shards[shard_of(s["start_url"], workers)].append(s)
    procs = [ctx.Process(target=_worker_main, args=(sh, opts, out), daemon=True) for sh in shards if sh]
    for p in procs:
        p.start()
    finished = got = 0
    try:
        while finished < len(procs):
            try:
                kind, payload = out.get(timeout=1.0)
            except queue_mod.Empty:
                if not any(p.is_alive() for p in procs):
                    log.error("Processus d'exploration interrompus avant la fin.")
                    break
                continue
            if kind == "result":
                got += 1
                on_result(payload)
                if on_progress:
                    on_progress(got, len(sites), payload.row)
            elif kind == "metrics":
                if on_metrics:
                    on_metrics(payload)
            elif kind == "done":
                finished += 1
            elif kind == "error":
                log.error("Erreur dans un processus d'exploration : %s", payload)
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=5)


# ----------------------------------------------------------------- orchestration / E-S
def read_csv(path: Path) -> list[dict]:
    return csvio.read_rows(path)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    csvio.write_rows(path, rows, fields)


def candidate_rows(paths: DeptPaths) -> list[dict] | None:
    """URL candidates du département : inventaire (DILA) + sites devinés. None si l'inventaire manque."""
    if not paths.inventory_urls.exists():
        return None
    inv = [r for r in csvio.read_rows(paths.inventory_urls) if r.get("statut_url") == "url_non_verifiee" and r.get("url_candidate")]
    shared: dict[str, set] = {}
    for r in inv:
        shared.setdefault(r["url_candidate"], set()).add(r["code_insee"])
    rows = [{"code_insee": r["code_insee"], "nom_commune": r["nom_commune"], "url_candidate": r["url_candidate"],
             "confiance": assess_confidence(r["nom_commune"], r["url_candidate"], None, "accessible",
                                            len(shared[r["url_candidate"]]))[0]} for r in inv]
    if paths.discovered.exists():
        rows += [{"code_insee": r["code_insee"], "nom_commune": r["nom_commune"], "url_candidate": r["url_candidate"],
                  "confiance": "devine_a_verifier"} for r in csvio.read_rows(paths.discovered) if r.get("url_candidate")]
    return rows


def summarize(site_rows: list[dict], link_rows: list[dict], params: dict, checked_at: str) -> dict:
    def count(rows, key):
        out: dict[str, int] = {}
        for r in rows:
            out[str(r[key])] = out.get(str(r[key]), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def num(r, k):
        try:
            return int(r.get(k) or 0)
        except ValueError:
            return 0
    explored = [r for r in site_rows if r.get("statut_crawl") != "non_explore"]
    found = [r for r in explored if r.get("statut_crawl") == "lien_trouve"]
    communes_links = {r["code_insee"] for r in link_rows}
    return {
        "checked_at_utc": checked_at, "cible": f"https://{TARGET_HOST}", "parametres": params,
        "sites_total": len(site_rows), "par_statut_crawl": count(site_rows, "statut_crawl"),
        "liens_trouves": len(link_rows), "communes_avec_lien": len(communes_links),
        "communes_avec_lien_sur_site_ssl_invalide": len({r["code_insee"] for r in link_rows if str(r.get("ssl_invalide")) == "1"}),
        "communes_avec_lien_sur_site_devine": len({r["code_insee"] for r in link_rows if r.get("confiance_site") == "devine_a_verifier"}),
        "pages_visitees_total": sum(num(r, "pages_visitees") for r in site_rows),
        "requetes_http_total": sum(num(r, "requetes_http") for r in site_rows),
        "pages_moyennes_avant_lien": round(sum(num(r, "pages_visitees") for r in found) / len(found), 1) if found else None,
        "sites_retentes": sum(num(r, "tentatives") > 1 for r in explored),
        "sites_recuperes_par_nouvelle_tentative": sum(num(r, "tentatives") > 1 and r.get("statut_crawl") != "erreur_acces"
                                                      for r in explored),
        "pages_tronquees_total": sum(num(r, "pages_tronquees") for r in explored),
        "limitations": [
            "L'exploration s'arrête au premier lien trouvé : un site « lien_trouve » n'est pas lu en entier (choix).",
            "Pour un site sans lien trouvé, seules les pages atteignables dans les limites de profondeur et de pages sont "
            "lues : « aucun lien » ne signifie pas « absent » si le site est en statut explore_partiel.",
            "Parcours en largeur sans priorité de contenu : les pages sont lues dans l'ordre où elles apparaissent.",
            "Sites JavaScript (rendu côté client) : liens non vus s'ils ne figurent pas dans le code de la page (pas de navigateur) ; l'accueil vide est signalé dans accueil_diag.",
            "Les redirections ou raccourcisseurs menant au site cible ne sont pas suivis.",
            "Sites à certificat invalide explorés sans vérification du certificat (ssl_invalide=1) : contenu non authentifié.",
            "Sites « devine_a_verifier » : trouvés par noms de domaine usuels, non référencés par la DILA.",
            "robots.txt est respecté ; un robots.txt en erreur HTTP (5xx) empêche l'exploration du site."]}


def _prepare(paths: DeptPaths, args) -> dict | None:
    rows = candidate_rows(paths)
    if rows is None:
        log.error("[%s] %s introuvable : lancez d'abord l'étape inventory", paths.dept, paths.inventory_urls)
        return None
    todo, skipped = select_sites(rows, tuple(args.include))
    if args.commune:
        todo = [s for s in todo if s["code_insee"] in set(args.commune)]
    if args.limit_sites:
        todo = todo[: args.limit_sites]
    previous_sites = read_csv(paths.crawl_sites) if paths.crawl_sites.exists() else []
    previous_links = read_csv(paths.links_found) if paths.links_found.exists() else []

    kept_sites: list[dict] = []
    kept_links: list[dict] = []
    if args.commune or args.limit_sites:                             # essai partiel : conserve les autres sites
        sel = {(s["code_insee"], s["start_url"]) for s in todo}
        kept_sites = [r for r in previous_sites
                      if r["statut_crawl"] != "non_explore" and (r["code_insee"], r["site_url"]) not in sel]
        out_keys = {(r["code_insee"], r["site_url"]) for r in kept_sites}
        kept_links = [r for r in previous_links if (r["code_insee"], r["site_url"]) in out_keys]
    if args.resume and previous_sites:
        def num(r, k):
            try:
                return int(r.get(k) or 0)
            except ValueError:
                return 0

        def settled(r: dict) -> bool:
            """Terminé, sauf échec à retenter ou exploration partielle dont les limites ont depuis été relevées."""
            st = r["statut_crawl"]
            if st in RETRY_STATUTS | {"non_explore"}:
                return False
            if st == "explore_complet" and "accueil_diag" not in r and num(r, "pages_visitees") <= 2:
                return False                    # lu avant le plan B (accueil sans lien) : à refaire une fois
            if st == "explore_partiel":
                if "durée maximale" in r.get("motif", "") and args.site_timeout > float(r.get("duree_s") or 0) + 1:
                    return False
                if str(r.get("limite_pages_atteinte")) == "1" and num(r, "pages_visitees") < args.max_pages:
                    return False
                if str(r.get("limite_profondeur_atteinte")) == "1" and num(r, "profondeur_max_atteinte") < args.max_depth:
                    return False
            return True
        done = {(r["code_insee"], r["site_url"]) for r in previous_sites if settled(r)}
        todo_keys = {(s["code_insee"], s["start_url"]) for s in todo}
        kept_sites += [r for r in previous_sites if (r["code_insee"], r["site_url"]) in done & todo_keys]
        kept_links += [r for r in previous_links if (r["code_insee"], r["site_url"]) in done & todo_keys]
        todo = [s for s in todo if (s["code_insee"], s["start_url"]) not in done]

    known: dict[tuple[str, str], list[str]] = {}
    if getattr(args, "recheck_known", True):                         # page où le lien avait été vu : revérifiée en premier
        for r in previous_links:
            if r.get("type_lien", "lien") in CLICKABLE and r.get("page_url"):
                pages = known.setdefault((r["code_insee"], r["site_url"]), [])
                if r["page_url"] not in pages:
                    pages.append(r["page_url"])
    prev_duration = {}
    for r in previous_sites:
        try:
            prev_duration[(r["code_insee"], r["site_url"])] = float(r.get("duree_s") or 0)
        except ValueError:
            pass
    # Les sites les plus lents lors du passage précédent démarrent en premier : leur durée se superpose au reste du travail
    # au lieu de s'ajouter en fin de département (ordonnancement « plus long d'abord »). Sans historique : ordre inchangé.
    todo.sort(key=lambda s: -prev_duration.get((s["code_insee"], s["start_url"]), 5.0))
    for s in todo:
        s["_dept"] = paths.dept
        s["known_pages"] = known.get((s["code_insee"], s["start_url"]), [])
    log.info("[%s] %d sites à explorer (%d écartés, %d conservés, %d avec page connue)", paths.dept, len(todo),
             len(skipped), len(kept_sites), sum(bool(s["known_pages"]) for s in todo))
    return {"paths": paths, "todo": todo, "skipped": skipped, "kept_sites": kept_sites, "kept_links": kept_links}


def _finalize(prep: dict, results: list[SiteResult], params: dict, checked_at: str, quiet: bool = False) -> dict:
    """Écrit crawl_sites.csv, links_found.csv et crawl_summary.json du département."""
    paths = prep["paths"]
    for r in results:                                              # confiance recalculée sur l'URL réellement lue
        row = r.row
        if row.get("url_utilisee"):
            row["confiance_finale"] = assess_confidence(row["nom_commune"], row["site_url"], row["url_utilisee"],
                                                        "accessible", 1, redirect_type(row["site_url"], row["url_utilisee"]))[0]
    skipped_rows = [{**{k: "" for k in SITE_FIELDS}, "code_insee": s["code_insee"], "nom_commune": s["nom_commune"],
                     "site_url": s["start_url"], "confiance_lot2": s["confiance_lot2"], "statut_crawl": "non_explore",
                     "motif": s["motif"], "date_controle": checked_at} for s in prep["skipped"]]
    site_rows = prep["kept_sites"] + [r.row for r in results] + skipped_rows
    link_rows = prep["kept_links"] + [l for r in results for l in r.links]
    write_csv(paths.crawl_sites, site_rows, SITE_FIELDS)
    write_csv(paths.links_found, link_rows, LINK_FIELDS)
    summary = summarize(site_rows, link_rows, params, checked_at)
    paths.crawl_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not quiet:
        print(f"\n[{paths.dept}] Exploration terminée : {len(results)} site(s) explorés, {len(prep['skipped'])} écartés.")
        print("  Statuts :", summary["par_statut_crawl"])
        explored_n = len([r for r in site_rows if r.get("statut_crawl") != "non_explore"])
        failed_n = summary["par_statut_crawl"].get("erreur_acces", 0)
        if explored_n and failed_n / explored_n > 0.10:
            print(f"  ATTENTION : {failed_n} site(s) sur {explored_n} n'ont pas pu être lus (erreur d'accès). Des liens "
                  "peuvent manquer : réduisez le parallélisme (config.toml) puis relancez avec la reprise.")
        cut = sum("durée maximale" in r.get("motif", "") for r in site_rows)
        if explored_n and cut / explored_n > 0.10 and params.get("host_delay"):
            reachable = int(params["site_timeout"] / params["host_delay"])
            print(f"  NOTE : {cut} site(s) coupés par la durée maximale ({params['site_timeout']:.0f} s ÷ {params['host_delay']} s de "
                  f"politesse ≈ {reachable} pages au plus, quelle que soit la limite de {params['max_pages']} pages). Relever "
                  "`duree_max_par_site_s` augmente le nombre de pages lues, donc le rappel, mais allonge l'exploration.")
        blank = sum(bool(r.get("accueil_diag")) for r in site_rows)
        if blank:
            print(f"  NOTE : {blank} site(s) dont l'accueil n'a aucun lien interne (JavaScript, protection anti-robot…) : "
                  "voir la colonne accueil_diag de crawl_sites.csv.")
        print(f"  Liens trouvés : {summary['liens_trouves']} dans {summary['communes_avec_lien']} commune(s) ; "
              f"pages moyennes avant lien : {summary['pages_moyennes_avant_lien']} ; requêtes HTTP : {summary['requetes_http_total']}")
    return summary


def run_many(paths_list: list[DeptPaths], args, on_dept_done=None) -> int:
    """Explore les sites de tous les départements dans UN SEUL pool ; écrit les résultats d'un département dès que
    tous ses sites sont traités (puis appelle on_dept_done(paths))."""
    preps = [p for p in (_prepare(pl, args) for pl in paths_list) if p is not None]
    rc = 0 if len(preps) == len(paths_list) else 1
    params = {k: getattr(args, k, None) for k in ("max_pages", "max_depth", "concurrency", "processes", "host_delay",
                                                 "timeout", "site_timeout", "max_par_domaine", "max_par_processus", "dns_simultanees", "include",
                                                 "resume", "no_insecure_ssl", "use_sitemap")}
    states = {p["paths"].dept: {"prep": p, "results": [], "remaining": len(p["todo"]), "since": 0, "done": False}
              for p in preps}
    todo_all = [s for p in preps for s in p["todo"]]
    opts = {"max_pages": args.max_pages, "max_depth": args.max_depth, "concurrency": args.concurrency,
            "host_delay": args.host_delay, "timeout": args.timeout, "site_timeout": args.site_timeout,
            "contact": args.contact, "use_sitemap": getattr(args, "use_sitemap", False),
            "retry_failed": getattr(args, "retry_failed", True), "insecure_ssl": not args.no_insecure_ssl,
            "max_par_domaine": getattr(args, "max_par_domaine", 4),
            "max_per_process": getattr(args, "max_par_processus", MAX_PER_PROCESS),
            "dns_simultanees": getattr(args, "dns_simultanees", 48),
            "metrics_interval": getattr(args, "metrics_interval", 10.0)}
    perf_path = None
    on_metrics = None
    if getattr(args, "metrics", True) and todo_all:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        perf_path = paths_list[0].root / "data" / "performance" / f"perf-{stamp}.csv"
        perf_path.parent.mkdir(parents=True, exist_ok=True)
        perf_path.with_suffix(".json").write_text(json.dumps(
            {"machine": tuning.machine_info(), "reglages": {**params, "include": None},
             "departements": [p.dept for p in paths_list]}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        def on_metrics(row: dict) -> None:
            csvio.append_row(perf_path, row, mt.FIELDS)

    def finish(dept: str, partial: bool = False) -> None:
        st = states[dept]
        _finalize(st["prep"], st["results"], params, datetime.now(timezone.utc).isoformat(), quiet=partial)
        if not partial:
            st["done"] = True
            if on_dept_done is not None:
                on_dept_done(st["prep"]["paths"])

    for dept, st in states.items():                                  # département sans site à explorer : écriture immédiate
        if st["remaining"] == 0:
            finish(dept)

    def on_result(res: SiteResult) -> None:
        st = states[res.dept]
        st["results"].append(res)
        st["remaining"] -= 1
        st["since"] += 1
        total_done = sum(len(s["results"]) for s in states.values())
        if total_done % 25 == 0 or st["remaining"] == 0:
            log.info("  [%s] %d/%d sites ; total %d/%d", res.dept, len(st["results"]), len(st["prep"]["todo"]),
                     total_done, len(todo_all))
        if st["remaining"] == 0:
            finish(res.dept)                                          # fin du département : on écrit le résultat
        elif st["since"] >= CHECKPOINT_EVERY:
            st["since"] = 0
            finish(res.dept, partial=True)                            # point de sauvegarde

    workers = getattr(args, "processes", 1) or 1
    try:
        if todo_all:
            crawl_sites(todo_all, opts, on_result, workers, on_metrics=on_metrics)
            if perf_path is not None and perf_path.exists():
                try:
                    tuning.report(perf_path)
                except Exception as exc:  # noqa: BLE001 - un rapport de performance ne doit jamais faire échouer l'exploration
                    log.warning("Analyse des mesures impossible : %s", exc)
    except KeyboardInterrupt:
        log.error("Interrompu : écriture des résultats partiels (reprise possible avec --resume)…")
        for dept, st in states.items():
            if not st["done"]:
                finish(dept, partial=True)
        return 130
    for dept, st in states.items():
        if not st["done"]:
            log.warning("[%s] %d site(s) sans résultat (processus interrompu ?) : relancez avec --resume",
                        dept, st["remaining"])
            finish(dept)
            rc = 1
    return rc


def run(paths: DeptPaths, args) -> int:
    return run_many([paths], args)
