"""Données et serveur simulé communs aux tests (aucun accès au vrai réseau)."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

T = "https://vosquestions.ecologie-territoires.gouv.fr"
HTML = {"content-type": "text/html; charset=utf-8"}

COG = """TYPECOM,COM,REG,DEP,CTCD,ARR,CAN,COMPARENT,LIBELLE
COM,60001,32,60,60D,601,6001,,Commune A
COM,60002,32,60,60D,601,6001,,Commune B
COM,01001,84,01,01D,011,0101,,L'Abergement-Clemenciat
COM,2A004,94,2A,2AD,2A1,2A01,,Ajaccio
COM,75056,11,75,75C,751,7501,,Paris
ARM,75101,11,75,75C,751,7501,75056,Paris 1er Arrondissement
ARM,75102,11,75,75C,751,7501,75056,Paris 2e Arrondissement
COMD,60099,32,60,60D,601,6001,60001,Ancienne Commune
"""


import crawl_links  # noqa: E402


async def _dns_ok(host, timeout=5.0):
    return ""


# Les tests simulent le réseau : la résolution DNS préalable ne doit pas interroger le vrai DNS (les tests de cette
# résolution la remplacent eux-mêmes).
crawl_links.resolve = _dns_ok


def page(body: str) -> httpx.Response:
    return httpx.Response(200, headers=HTML, text=body)


def web(request: httpx.Request) -> httpx.Response:
    """Deux sites : communea.fr (lien sur /risques, niveau 1) et abergement-clemenciat.fr (lien à l'accueil)."""
    u = str(request.url)
    if u.endswith("/robots.txt") or u.endswith("/sitemap.xml"):
        return httpx.Response(404)
    pages = {"https://www.communea.fr/": '<a href="/actualites">Actualités</a><a href="/risques">Risques</a>',
             "https://www.communea.fr/actualites": "<p>rien</p>",
             "https://www.communea.fr/risques": f'<a href="{T}/60001-commune-a">Vos questions</a>',
             "https://www.abergement-clemenciat.fr/": f'<a href="{T}/">Vos questions</a>'}
    return page(pages[u]) if u in pages else httpx.Response(404)


def dila(dept: str) -> list[dict]:
    def rec(nom, code, url):
        return {"nom": nom, "code_insee_commune": code, "site_internet": json.dumps([{"valeur": url}]) if url else "",
                "pivot": json.dumps([{"type_service_local": "mairie", "code_insee_commune": [code]}])}
    return {"60": [rec("Mairie - Commune A", "60001", "https://www.communea.fr/"), rec("Mairie - Commune B", "60002", "")],
            "01": [rec("Mairie - L'Abergement-Clemenciat", "01001", "https://www.abergement-clemenciat.fr/")]}[dept]


def patch_network(handler=web):
    """Remplace httpx.AsyncClient par un client branché sur `handler`. Retourne une fonction de restauration."""
    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(handler)})
    return lambda: setattr(httpx, "AsyncClient", orig)
