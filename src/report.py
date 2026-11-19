"""Lot 5 - Compte rendu HTML d'un département (fichier autonome, lisible sans le programme).

Pages en charte DSFR 1.15.4 (ressources locales dans src/static/dsfr, aucun CDN).
"""
from __future__ import annotations

import os
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

import coverage
import crawl_links as cl
import history
import results
from layout import DeptPaths

STATIC_DSFR = Path(__file__).resolve().parent / "static" / "dsfr"
STATIC_PREFIX_WEB = "/static/dsfr"  # chemin servi par webapp.py
# Seule règle propre au projet : les journaux (pre) doivent passer à la ligne.
STYLE_PROJET = ".mlm-journal { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .875rem; }"
MINISTERES = ("Ministères<br>Transition écologique<br>Aménagement du territoire<br>Transports<br>Ville et Logement")
ALERTE_PAR_CLASSE = {"ok": "success", "alerte": "error", "attention": "warning"}


def header(accueil: str | None) -> str:
    titre = '<p class="fr-header__service-title">Mairie Link Monitor</p>'
    if accueil:
        titre = f'<a href="{accueil}" title="Accueil - Mairie Link Monitor">{titre}</a>'
    return ('<header role="banner" class="fr-header"><div class="fr-header__body"><div class="fr-container">'
            '<div class="fr-header__body-row"><div class="fr-header__brand fr-enlarge-link">'
            '<div class="fr-header__brand-top"><div class="fr-header__logo">'
            f'<p class="fr-logo">{MINISTERES}</p></div></div>'
            f'<div class="fr-header__service">{titre}'
            '<p class="fr-header__service-tagline">Suivi des liens vers Vos Questions dans les sites de mairies</p>'
            "</div></div></div></div></div></header>")


def footer() -> str:
    return ('<footer class="fr-footer" role="contentinfo" id="footer"><div class="fr-container"><div class="fr-footer__body">'
            '<div class="fr-footer__content"><p class="fr-footer__content-desc">Outil de suivi des liens vers '
            "Vos Questions dans les sites de mairies.</p></div></div></div></footer>")


def page(title: str, body: str, refresh: int | None = None, static_prefix: str = STATIC_PREFIX_WEB) -> str:
    sp = escape(static_prefix)
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    # Lien d'accueil seulement sur le web : un fichier écrit sur disque n'a pas de racine « / ».
    accueil = "/" if static_prefix.startswith("/") else None
    return (
        '<!doctype html><html lang="fr" data-fr-scheme="system"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{meta}<title>{escape(title)}</title>"
        '<meta name="theme-color" content="#000091">'
        f'<link rel="apple-touch-icon" href="{sp}/favicon/apple-touch-icon.png">'
        f'<link rel="icon" href="{sp}/favicon/favicon.svg" type="image/svg+xml">'
        f'<link rel="shortcut icon" href="{sp}/favicon/favicon.ico" type="image/x-icon">'
        f'<link rel="manifest" href="{sp}/favicon/manifest.webmanifest">'
        f'<link rel="stylesheet" href="{sp}/dsfr/dsfr.min.css">'
        f"<style>{STYLE_PROJET}</style>"
        "</head><body>"
        '<div class="fr-skiplinks"><nav class="fr-container" role="navigation" aria-label="Accès rapide">'
        '<ul class="fr-skiplinks__list"><li><a class="fr-link" href="#content">Aller au contenu</a></li></ul>'
        "</nav></div>"
        f"{header(accueil)}"
        f'<main id="content" role="main"><div class="fr-container fr-my-4w">{body}</div></main>'
        f"{footer()}"
        f'<script type="module" src="{sp}/dsfr.module.min.js"></script>'
        f'<script nomodule src="{sp}/dsfr.nomodule.min.js"></script>'
        "</body></html>"
    )


def link(url: str) -> str:
    return f'<a href="{escape(url)}" rel="noopener noreferrer" target="_blank">{escape(url)}</a>' if url else ""


def source(url: str) -> str:
    return "PanneauPocket" if (urlsplit(url).hostname or "") == cl.PP_HOST else "site de la mairie"


def table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="fr-hint-text">{escape(empty)}</p>'
    head = "".join(f'<th scope="col">{escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return ('<div class="fr-table"><div class="fr-table__wrapper"><div class="fr-table__container">'
            f'<div class="fr-table__content"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
            "</div></div></div></div>")


def fr_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y à %H:%M")
    except ValueError:
        return iso


def render(paths: DeptPaths, db: Path, static_prefix: str = STATIC_PREFIX_WEB) -> str:
    states = coverage.build(paths) if paths.inventory_communes.exists() else []
    try:
        links = {r["code_insee"]: r["url_page"] for r in results.collect(paths)}
    except FileNotFoundError:
        links = {}
    ch = history.changes(db, paths.dept)
    n = len(states)
    count = lambda pred: sum(1 for s in states if pred(s))  # noqa: E731
    names = {s["code_insee"]: s["nom_commune"] for s in states}
    when = fr_date(ch["courant"]["date"]) if ch["courant"] else "pas encore de contrôle enregistré"
    figures = [(n, "communes"), (count(lambda s: s["analysee"] == "oui"), "communes analysées"),
               (len(links), "communes avec lien"),
               (count(lambda s: s["etat"] == "lien trouvé (PanneauPocket)"), "dont via PanneauPocket"),
               (count(lambda s: s["etat"].startswith("aucun site")), "sans site trouvé"),
               (count(lambda s: s["etat"].startswith("site non lu")), "sites illisibles")]
    chiffres = "".join(f'<div class="fr-col-6 fr-col-md-4 fr-col-lg-2"><div class="fr-callout">'
                       f'<p class="fr-callout__title">{v}</p><p class="fr-callout__text">{escape(t)}</p>'
                       "</div></div>" for v, t in figures)
    body = [f"<h1>Compte rendu – département {escape(paths.dept)}</h1>",
            f'<p class="fr-text--lead">Liens vers vosquestions.ecologie-territoires.gouv.fr — contrôle du {escape(when)}</p>',
            f'<div class="fr-grid-row fr-grid-row--gutters">{chiffres}</div>']

    body.append("<h2>Changements depuis le contrôle précédent</h2>")
    if ch["precedent"] is None:
        body.append('<p class="fr-hint-text">Premier contrôle enregistré : les changements apparaîtront au prochain.</p>')
    else:
        body.append(f'<p class="fr-hint-text">Comparaison avec le contrôle du {escape(fr_date(ch["precedent"]["date"]))}.</p>')
        sections = [("nouveaux", "Nouveaux liens", "ok", ["Commune", "Page"], lambda r: [escape(r["nom_commune"]), link(r["page"])]),
                    ("disparus_confirmes", "Liens disparus (confirmé : site lu sans le lien)", "alerte",
                     ["Commune", "Page où il était"], lambda r: [escape(r["nom_commune"]), link(r["page"])]),
                    ("disparus_a_verifier", "Liens non retrouvés, à vérifier (site non lu en entier)", "attention",
                     ["Commune", "Page où il était", "Raison"],
                     lambda r: [escape(r["nom_commune"]), link(r["page"]), escape(r["etat_actuel"])]),
                    ("deplaces", "Liens trouvés sur une autre page", "", ["Commune", "Nouvelle page", "Ancienne page"],
                     lambda r: [escape(r["nom_commune"]), link(r["page"]), link(r["page_precedente"])])]
        for key, title, cls, headers, fmt in sections:
            rows = ch[key]
            titre = f"{escape(title)} : {len(rows)}"
            if cls:
                body.append(f'<div class="fr-alert fr-alert--{ALERTE_PAR_CLASSE[cls]} fr-mb-2w">'
                            f'<h3 class="fr-alert__title">{titre}</h3></div>')
            else:
                body.append(f"<h3>{titre}</h3>")
            if rows:
                body.append(table(headers, [fmt(r) for r in rows], ""))

    body.append(f"<h2>Communes avec un lien ({len(links)})</h2>")
    body.append(table(["Code INSEE", "Commune", "Page", "Source"],
                      [[escape(c), escape(names.get(c, "")), link(u),
                        f'<span class="fr-badge fr-badge--info fr-badge--sm">{escape(source(u))}</span>']
                       for c, u in sorted(links.items())],
                      "Aucun lien trouvé."))

    unread = [s for s in states if s["analysee"] == "non" or s["etat"].startswith("site lu partiellement")]
    body.append("<h2>À vérifier</h2>")
    body.append(f"<details><summary>{len(unread)} commune(s) non lue(s) ou lue(s) partiellement</summary>"
                + table(["Code INSEE", "Commune", "État", "Détail"],
                        [[escape(s["code_insee"]), escape(s["nom_commune"]), escape(s["etat"]), escape(s["motif"])]
                         for s in unread], "Aucune.") + "</details>")
    body.append('<p class="fr-hint-text">« Aucun lien » n\'est certain que pour un site lu en entier (dans les limites fixées) '
                "ou une page PanneauPocket lue. Les sites construits en JavaScript ne sont pas lus.</p>")
    return page(f"Compte rendu {paths.dept}", "\n".join(body), static_prefix=static_prefix)


def write(paths: DeptPaths, db: Path) -> Path:
    out = paths.results_dir / f"rapport_{paths.dept}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Fichier autonome : chemin relatif vers les ressources DSFR locales.
    prefix = Path(os.path.relpath(STATIC_DSFR, out.parent.resolve())).as_posix()
    out.write_text(render(paths, db, static_prefix=prefix), encoding="utf-8")
    return out
