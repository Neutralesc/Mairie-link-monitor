"""Découverte des sites des communes sans site référencé dans la DILA.

Méthode : noms de domaine usuels construits depuis le nom de la commune (ex. nom.fr, mairie-nom.fr, commune-nom.fr,
nom60.fr…). Un domaine n'est retenu que si la page d'accueil, lue après vérification de robots.txt, contient
(1) le nom de la commune, (2) un terme de mairie/commune et (3) un code postal du département.
Résultat : reports/<dep>/sites_devines.csv, explorés ensuite avec la confiance « devine_a_verifier ».
C'est une HEURISTIQUE : un site deviné n'est jamais présenté comme officiel confirmé.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

import csvio
import crawl_links as cl
from layout import DeptPaths, normalize_dept

log = logging.getLogger("discover")
FIELDS = ["code_insee", "nom_commune", "url_candidate", "statut_url", "confiance", "motif", "candidats_testes", "rejets",
          "date_decouverte"]
ARTICLES = {"le", "la", "les", "l"}
PATTERNS = ("{v}.fr", "mairie-{v}.fr", "mairie{v}.fr", "commune-{v}.fr", "ville-{v}.fr", "{v}{dept}.fr", "{v}.com",
            "mairie-de-{v}.fr", "mairiede{v}.fr")
MAIRIE_RE = re.compile(r"\b(mairie|commune|municipal\w*|conseil municipal|ville de)\b")


def slug_tokens(name: str) -> list[str]:
    text = name.replace("œ", "oe").replace("Œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", text)


def name_variants(name: str) -> list[str]:
    """Formes de nom utilisables dans un domaine : avec tirets / compacte, avec et sans article initial."""
    toks = slug_tokens(name)
    if not toks:
        return []
    no_art = toks[1:] if toks[0] in ARTICLES and len(toks) > 1 else toks
    variants = []
    for t in (toks, no_art):
        for v in ("-".join(t), "".join(t)):
            if v and v not in variants:
                variants.append(v)
    return variants[:4]


def candidate_hosts(name: str, dept: str) -> list[str]:
    d = dept.lower() if dept.isdigit() and len(dept) == 2 else ""
    hosts: list[str] = []
    for v in name_variants(name):
        for pat in PATTERNS:
            if "{dept}" in pat and not d:
                continue
            h = pat.format(v=v, dept=d)
            if h not in hosts and len(h) <= 60:
                hosts.append(h)
    return hosts


def postal_regex(dept: str) -> re.Pattern:
    if dept in ("2A", "2B"):
        return re.compile(r"(?<!\d)20\d{3}(?!\d)")
    if len(dept) == 3:
        return re.compile(rf"(?<!\d){dept}\d{{2}}(?!\d)")
    return re.compile(rf"(?<!\d){dept}\d{{3}}(?!\d)")


def validate_home(html: str, name: str, dept: str) -> tuple[bool, str]:
    try:
        tree = LexborHTMLParser(html)
        text = tree.text(separator=" ")[:300_000]
        title = (tree.css_first("title").text() if tree.css_first("title") else "")
    except Exception:  # noqa: BLE001
        return False, "page inanalysable"
    norm = " " + " ".join(slug_tokens(f"{title} {text}")) + " "
    toks = slug_tokens(name)
    forms = {" ".join(toks), " ".join(toks[1:] if toks and toks[0] in ARTICLES else toks)}
    forms |= {f.replace("saint", "st") for f in list(forms)}
    if not any(f and f" {f} " in norm for f in forms):
        return False, "nom de la commune absent"
    if not MAIRIE_RE.search(norm):
        return False, "aucun terme mairie/commune"
    if not postal_regex(dept).search(f"{title} {text}"):
        return False, f"aucun code postal du département {dept}"
    return True, f"nom de la commune + mairie/commune + code postal {dept}xxx"


async def default_resolver(host: str) -> bool:
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), 4)
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def probe(client, commune: dict, dept: str, resolver, throttle, timeout: float) -> dict:
    """Teste les noms de domaine usuels d'une commune ; retourne la ligne de résultat (url vide si rien de valide)."""
    tested, rejets = 0, []
    for host in candidate_hosts(commune["nom_commune"], dept):
        if not await resolver(host):
            continue
        tested += 1
        for scheme in ("https", "http"):
            origin = f"{scheme}://{host}"
            rp, state, kind, status = await cl.load_robots(client, origin + "/", cl.norm_host(host), throttle, timeout)
            if state == "indisponible" or (rp is not None and not rp.can_fetch(cl.UA_TOKEN, origin + "/")):
                rejets.append(f"{host}: robots.txt")
                continue
            res = await cl.fetch(client, origin + "/", cl.norm_host(host), throttle, timeout=timeout, follow_external=True)
            if res.kind != "accessible":
                rejets.append(f"{host}: {res.kind}")
                continue
            ok, why = validate_home(res.html, commune["nom_commune"], dept)
            if ok:
                return {"code_insee": commune["code_insee"], "nom_commune": commune["nom_commune"],
                        "url_candidate": res.final_url or origin + "/", "statut_url": "url_non_verifiee",
                        "confiance": "devine_a_verifier", "motif": why, "candidats_testes": tested, "rejets": ""}
            rejets.append(f"{host}: {why}")
            break                                              # domaine existant mais autre site : on passe au suivant
    return {"code_insee": commune["code_insee"], "nom_commune": commune["nom_commune"], "url_candidate": "",
            "statut_url": "", "confiance": "aucun_site_trouve", "motif": f"{tested} domaine(s) résolu(s), aucun valide",
            "candidats_testes": tested, "rejets": " | ".join(rejets[:6])}


async def discover_all(communes: list[tuple[str, dict]], concurrency: int, timeout: float, contact: str = "",
                       transport=None, resolver=default_resolver) -> list[tuple[str, dict]]:
    cl.tune_runtime(concurrency)
    ua = f"{cl.UA_BASE} {contact}".strip()
    throttle = cl.Throttle(0.2)
    todo: asyncio.Queue = asyncio.Queue()
    for item in communes:
        todo.put_nowait(item)
    out: list[tuple[str, dict]] = []
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, transport=transport, headers={"User-Agent": ua},
                                 limits=httpx.Limits(max_connections=concurrency * 2 + 10)) as client:
        async def worker() -> None:
            while True:
                try:
                    dept, c = todo.get_nowait()
                except asyncio.QueueEmpty:
                    return
                out.append((dept, await probe(client, c, dept, resolver, throttle, timeout)))
        await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, len(communes))))))
    return out


def run_many(paths_list: list[DeptPaths], args, transport=None, resolver=default_resolver) -> int:
    """Cherche les sites des communes sans URL (tous départements dans un seul pool), écrit sites_devines.csv par département."""
    items: list[tuple[str, dict]] = []
    existing: dict[str, list[dict]] = {}
    for paths in paths_list:
        if not paths.inventory_communes.exists():
            log.error("[%s] inventaire absent : lancez d'abord l'étape inventory", paths.dept)
            return 1
        prev = csvio.read_rows(paths.discovered) if paths.discovered.exists() else []
        existing[paths.dept] = prev if args.resume else []
        done = {r["code_insee"] for r in existing[paths.dept]}
        for c in csvio.read_rows(paths.inventory_communes):
            if int(c.get("nb_urls_valides") or 0) == 0 and c["code_insee"] not in done:
                items.append((paths.dept, c))
    log.info("Découverte de sites : %d commune(s) sans site référencé", len(items))
    results = asyncio.run(discover_all(items, args.concurrency, min(args.timeout, 8.0), args.contact, transport, resolver)) if items else []
    now = datetime.now(timezone.utc).isoformat()
    for paths in paths_list:
        rows = existing[paths.dept] + [{**r, "date_decouverte": now} for d, r in results if d == paths.dept]
        csvio.write_rows(paths.discovered, rows, FIELDS)
        found = sum(1 for r in rows if r.get("url_candidate"))
        print(f"[{paths.dept}] Découverte : {found} site(s) trouvé(s) pour {len([r for r in rows])} commune(s) testée(s)")
    return 0
