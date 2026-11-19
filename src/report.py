"""Lot 5 - Compte rendu HTML d'un département (fichier autonome, lisible sans le programme)."""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

import coverage
import crawl_links as cl
import history
import results
from layout import DeptPaths

STYLE = """
:root { --fond:#f6f7f9; --carte:#fff; --texte:#1c2430; --doux:#5a6472; --trait:#d9dee5; --accent:#1f5fbf;
        --ok:#1d7a3a; --alerte:#b3261e; --attention:#8a5a00; }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.5 system-ui, "Segoe UI", Arial, sans-serif; background:var(--fond); color:var(--texte); }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:1.6rem; margin:0 0 4px; } h2 { font-size:1.2rem; margin:32px 0 8px; }
p.sous-titre { color:var(--doux); margin:0 0 24px; }
.chiffres { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }
.chiffre { background:var(--carte); border:1px solid var(--trait); border-radius:8px; padding:12px 14px; }
.chiffre b { display:block; font-size:1.7rem; line-height:1.2; } .chiffre span { color:var(--doux); font-size:.9rem; }
.carte { background:var(--carte); border:1px solid var(--trait); border-radius:8px; padding:16px; margin-top:12px; }
table { width:100%; border-collapse:collapse; background:var(--carte); font-size:.95rem; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--trait); vertical-align:top; }
th { background:#eef1f5; font-weight:600; }
td a { word-break:break-all; }
a { color:var(--accent); } a:focus, button:focus, input:focus { outline:3px solid #9cc3ff; outline-offset:2px; }
.ok { color:var(--ok); font-weight:600; } .alerte { color:var(--alerte); font-weight:600; }
.attention { color:var(--attention); font-weight:600; }
details { margin-top:12px; } summary { cursor:pointer; font-weight:600; }
.note { color:var(--doux); font-size:.9rem; }
.table-defile { overflow-x:auto; }
.carte > h2:first-child { margin-top:0; }
button { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:8px 16px; font-size:1rem; cursor:pointer; }
"""


def page(title: str, body: str, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1">{meta}<title>{escape(title)}</title><style>{STYLE}</style>'
            f"</head><body><main>{body}</main></body></html>")


def link(url: str) -> str:
    return f'<a href="{escape(url)}" rel="noopener noreferrer" target="_blank">{escape(url)}</a>' if url else ""


def source(url: str) -> str:
    return "PanneauPocket" if (urlsplit(url).hostname or "") == cl.PP_HOST else "site de la mairie"


def table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="note">{escape(empty)}</p>'
    head = "".join(f'<th scope="col">{escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="table-defile"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def fr_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y à %H:%M")
    except ValueError:
        return iso


def render(paths: DeptPaths, db: Path) -> str:
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
    body = [f"<h1>Compte rendu – département {escape(paths.dept)}</h1>",
            f'<p class="sous-titre">Liens vers vosquestions.ecologie-territoires.gouv.fr — contrôle du {escape(when)}</p>',
            '<div class="chiffres">' + "".join(f'<div class="chiffre"><b>{v}</b><span>{escape(t)}</span></div>'
                                               for v, t in figures) + "</div>"]

    body.append("<h2>Changements depuis le contrôle précédent</h2>")
    if ch["precedent"] is None:
        body.append('<p class="note">Premier contrôle enregistré : les changements apparaîtront au prochain.</p>')
    else:
        body.append(f'<p class="note">Comparaison avec le contrôle du {escape(fr_date(ch["precedent"]["date"]))}.</p>')
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
            body.append(f'<h3 class="{cls}">{escape(title)} : {len(rows)}</h3>' if cls else f"<h3>{escape(title)} : {len(rows)}</h3>")
            if rows:
                body.append(table(headers, [fmt(r) for r in rows], ""))

    body.append(f"<h2>Communes avec un lien ({len(links)})</h2>")
    body.append(table(["Code INSEE", "Commune", "Page", "Source"],
                      [[escape(c), escape(names.get(c, "")), link(u), escape(source(u))] for c, u in sorted(links.items())],
                      "Aucun lien trouvé."))

    unread = [s for s in states if s["analysee"] == "non" or s["etat"].startswith("site lu partiellement")]
    body.append("<h2>À vérifier</h2>")
    body.append(f"<details><summary>{len(unread)} commune(s) non lue(s) ou lue(s) partiellement</summary>"
                + table(["Code INSEE", "Commune", "État", "Détail"],
                        [[escape(s["code_insee"]), escape(s["nom_commune"]), escape(s["etat"]), escape(s["motif"])]
                         for s in unread], "Aucune.") + "</details>")
    body.append('<p class="note">« Aucun lien » n\'est certain que pour un site lu en entier (dans les limites fixées) '
                "ou une page PanneauPocket lue. Les sites construits en JavaScript ne sont pas lus.</p>")
    return page(f"Compte rendu {paths.dept}", "\n".join(body))


def write(paths: DeptPaths, db: Path) -> Path:
    out = paths.results_dir / f"rapport_{paths.dept}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(paths, db), encoding="utf-8")
    return out
