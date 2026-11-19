"""Plateformes d'information communale : PanneauPocket.

Beaucoup de communes sans site publient sur PanneauPocket : une page par commune
(https://app.panneaupocket.com/ville/<numéro>-<nom>-<code postal>) qui affiche les annonces en cours, liens cliquables
compris, et renvoie vers les pages des communes voisines. Le numéro ne se déduit pas du nom, d'où les pages de départ :
champ `application_mobile` des fiches mairie de la DILA, liens PanneauPocket vus sur les sites des mairies, pages ajoutées
dans config.toml (`pages_panneaupocket`) et pages lues au passage précédent ; on suit ensuite les communes voisines du
même département. robots.txt et le délai par site sont respectés (un seul hôte : une requête à la fois).
Une page est rattachée à une commune par la DILA, sinon par son nom et son code postal ; une page qui n'est pas celle
d'une mairie (gendarmerie, intercommunalité…) est lue pour trouver d'autres communes mais jamais rattachée.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

import crawl_links as cl
import csvio
import inventory
from discover import slug_tokens
from layout import DeptPaths

log = logging.getLogger("platforms")
PLATFORM = "panneaupocket"
PAGE_FIELDS = ["code_insee", "nom_commune", "plateforme", "url_page", "source", "statut", "liens_trouves", "motif",
               "date_controle"]
NAME_PREFIXES = ("affichage-legal-", "mairie-de-", "mairie-d-", "mairie-", "commune-de-", "commune-d-", "ville-de-")
MAX_PAGES = 20000


def depts_of_cp(cp: str) -> set[str]:
    if cp.startswith("20"):
        return {"2A", "2B"}
    return {cp[:3]} if cp.startswith("97") else {cp[:2]}


def commune_index(paths_list: list[DeptPaths]) -> dict[str, list[tuple[str, str, str]]]:
    """nom en minuscules sans accents, mots séparés par « - » -> [(code_insee, nom, département)]"""
    index: dict[str, list[tuple[str, str, str]]] = {}
    for paths in paths_list:
        for c in csvio.read_rows(paths.inventory_communes):
            index.setdefault("-".join(slug_tokens(c["nom_commune"])), []).append((c["code_insee"], c["nom_commune"], paths.dept))
    return index


def match_page(url: str, index: dict) -> tuple[str, str, str] | None:
    """Commune d'une page PanneauPocket d'après son nom et son code postal ; None si absente ou ambiguë."""
    m = cl.PP_VILLE_RE.match(urlsplit(url).path)
    if not m:
        return None
    slug, cp = m.group(2), m.group(3)
    cands = index.get(slug) or next((index[slug[len(p):]] for p in NAME_PREFIXES
                                     if slug.startswith(p) and slug[len(p):] in index), [])
    in_dept = [c for c in cands if c[2] in depts_of_cp(cp)]
    pick = in_dept or cands
    return pick[0] if len(pick) == 1 else None


def seeds(paths: DeptPaths, extra: list[str]) -> list[tuple[str, str, str]]:
    """(url, code_insee connu ou '', source) des pages de départ du département."""
    out: list[tuple[str, str, str]] = []
    if paths.dila_records.exists():
        for r in csvio.read_rows(paths.dila_records):
            if inventory.service_type(r) not in ("mairie", "mairie_deleguee", "mairie_annexe"):
                continue
            codes = inventory.record_commune_codes(r)
            for v in inventory.flatten_values(r.get("application_mobile")):
                page = cl.panneaupocket_page(str(v).strip())
                if page and codes:
                    out.append((page, codes[0], "dila"))
    if paths.crawl_sites.exists():
        for r in csvio.read_rows(paths.crawl_sites):
            out += [(u, "", "site_mairie") for u in (r.get("pages_plateformes") or "").split(" | ") if u]
    if paths.platform_pages.exists():
        out += [(r["url_page"], r.get("code_insee", ""), "passage_precedent") for r in csvio.read_rows(paths.platform_pages)
                if r.get("source") != "voisine" or r.get("code_insee")]
    out += [(cl.panneaupocket_page(u), "", "config") for u in extra if cl.panneaupocket_page(u)]
    return out


async def walk(start: list[tuple[str, str, str]], index: dict, depts: set[str], opts: dict, transport=None) -> list[dict]:
    """Lit les pages de départ puis les communes voisines des départements choisis. Retourne une ligne par page
    (avec la liste de ses liens vers le portail cible)."""
    ua = f"{cl.UA_BASE} {opts.get('contact', '')}".strip()
    throttle = cl.Throttle(opts["host_delay"])
    now = datetime.now(timezone.utc).isoformat()
    queue, seen, pages = deque(), set(), []
    for url, code, source in start:
        if url not in seen:
            seen.add(url)
            queue.append((url, code, source))
    async with httpx.AsyncClient(follow_redirects=False, timeout=opts["timeout"], transport=transport,
                                 headers={"User-Agent": ua, "Accept": "text/html,*/*;q=0.5"}) as client:
        rp, state, kind, status = await cl.load_robots(client, f"https://{cl.PP_HOST}/", cl.PP_HOST, throttle, opts["timeout"])
        if state == "indisponible":
            log.error("PanneauPocket : robots.txt injoignable (%s %s) : aucune page lue", kind, status or "")
            return [{"url_page": u, "code_insee": c, "source": s, "statut": "robots_indisponible", "liens": [], "date_controle": now}
                    for u, c, s in queue]
        if rp is not None:
            cd = rp.crawl_delay(cl.UA_TOKEN) or rp.crawl_delay("*")
            if cd:
                throttle.per_host[cl.PP_HOST] = min(max(float(cd), throttle.delay), cl.MAX_CRAWL_DELAY)
        while queue and len(pages) < MAX_PAGES:
            url, code, source = queue.popleft()
            row = {"url_page": url, "code_insee": code, "source": source, "liens": [], "date_controle": now}
            pages.append(row)
            if rp is not None and not rp.can_fetch(cl.UA_TOKEN, url):
                row["statut"] = "robots_interdit"
                continue
            res = await cl.fetch(client, url, cl.PP_HOST, throttle, timeout=opts["timeout"])
            if res.kind != "accessible":
                row.update(statut="erreur", motif=f"{res.kind} {res.status or ''} {res.error}".strip()[:200])
                continue
            data = cl.parse_page(res.html, url, cl.PP_HOST)
            row.update(statut="lue", liens=data.links)
            for n in data.internal:
                page = cl.panneaupocket_page(n)
                m = cl.PP_VILLE_RE.match(urlsplit(page).path) if page else None
                if m and page not in seen and depts_of_cp(m.group(3)) & depts:
                    seen.add(page)
                    queue.append((page, "", "voisine"))
            if len(pages) % 25 == 0:
                log.info("  PanneauPocket : %d page(s) lue(s), %d en attente", len(pages), len(queue))
    return pages


def run_many(paths_list: list[DeptPaths], args, transport=None) -> int:
    """Lit les pages PanneauPocket des départements et écrit plateformes.csv et liens_plateformes.csv par département."""
    ok = [p for p in paths_list if p.inventory_communes.exists()]
    for p in paths_list:
        if p not in ok:
            log.error("[%s] inventaire absent : lancez d'abord l'étape inventory", p.dept)
    if not ok:
        return 1
    index = commune_index(ok)
    by_code = {c[0]: c for v in index.values() for c in v}
    start = [s for p in ok for s in seeds(p, getattr(args, "pages_panneaupocket", []) or [])]
    log.info("PanneauPocket : %d page(s) de départ", len({u for u, _, _ in start}))
    pages = asyncio.run(walk(start, index, {p.dept for p in ok}, {"host_delay": args.host_delay, "timeout": args.timeout,
                                                                   "contact": args.contact}, transport)) if start else []
    rows: dict[str, list[dict]] = {p.dept: [] for p in ok}
    links: dict[str, list[dict]] = {p.dept: [] for p in ok}
    for pg in pages:
        commune = by_code.get(pg["code_insee"]) or match_page(pg["url_page"], index)
        m = cl.PP_VILLE_RE.match(urlsplit(pg["url_page"]).path)
        dept = commune[2] if commune else next(iter(depts_of_cp(m.group(3)) & set(rows)), None) if m else None
        if dept is None:
            continue
        code, nom = (commune[0], commune[1]) if commune else ("", "")
        motif = pg.get("motif", "") or ("" if commune else "page non rattachée à une mairie (nom ou code postal sans correspondance)")
        rows[dept].append({"code_insee": code, "nom_commune": nom, "plateforme": PLATFORM, "url_page": pg["url_page"],
                           "source": pg["source"], "statut": pg.get("statut", ""), "liens_trouves": len(pg["liens"]) if commune else 0,
                           "motif": motif, "date_controle": pg["date_controle"]})
        if commune:
            links[dept] += [{"code_insee": code, "nom_commune": nom, "site_url": pg["url_page"], "page_url": pg["url_page"], **l,
                             "cible_commune": cl.target_scope(l["target_url"], code), "confiance_site": f"plateforme_{PLATFORM}",
                             "ssl_invalide": 0, "date_controle": pg["date_controle"]} for l in pg["liens"]]
    for p in ok:
        csvio.write_rows(p.platform_pages, rows[p.dept], PAGE_FIELDS)
        csvio.write_rows(p.platform_links, links[p.dept], cl.LINK_FIELDS)
        attached = [r for r in rows[p.dept] if r["code_insee"]]
        print(f"[{p.dept}] PanneauPocket : {len(rows[p.dept])} page(s) lue(s), {len(attached)} rattachée(s) à une commune, "
              f"{len({r['code_insee'] for r in links[p.dept]})} commune(s) avec lien")
    return 0
