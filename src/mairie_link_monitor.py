#!/usr/bin/env python3
"""mairie-link-monitor : programme unique.

Détecte, pour un ou plusieurs départements, les sites de mairies contenant un lien vers
https://vosquestions.ecologie-territoires.gouv.fr et exporte (code INSEE, URL de la page).

Lancement le plus simple : double-clic sur lancer.bat (ou `py -3 src\\mairie_link_monitor.py` sans argument) :
le programme demande les départements à traiter. Les réglages se trouvent dans config.toml.

Lancement avancé (les options de la ligne de commande l'emportent sur config.toml) :
  py -3 src\\mairie_link_monitor.py run --dept 60 80 2A
  py -3 src\\mairie_link_monitor.py export --dept 60
  py -3 src\\mairie_link_monitor.py migrate-legacy --dept 60 --source <ancien dossier>
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from argparse import Namespace
from pathlib import Path

import config as cfg
import coverage
import crawl_links
import csvio
import diagnostic
import discover
import history
import inventory
import platforms
import report
import results
import tuning
import verify_urls
from layout import DeptPaths, legacy_map, normalize_dept

log = logging.getLogger("mairie_link_monitor")
ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("run", "export", "reclassify", "migrate-legacy", "compare", "coverage", "tune", "web", "rapport", "historique",
            "publier")


# ----------------------------------------------------------------- départements
def resolve_depts(args, known: set[str] | None) -> list[str] | None:
    """Normalise args.dept, valide contre le COG si disponible. Retourne None en cas d'erreur."""
    requested = [str(d) for d in args.dept]
    if [d.lower() for d in requested] in (["all"], ["tous"]):
        if known is None:
            log.error("--dept all nécessite le COG (lancez d'abord l'étape inventory en ligne).")
            return None
        if not getattr(args, "confirm_all", False):
            log.error("--dept all explore %d départements (plusieurs dizaines de milliers de sites) : "
                      "ajoutez --confirm-all pour confirmer.", len(known))
            return None
        return sorted(known)
    depts: list[str] = []
    for d in requested:
        n = normalize_dept(d)
        if n not in depts:
            depts.append(n)
    if known is not None:
        unknown = [d for d in depts if d not in known]
        if unknown:
            log.error("Département(s) inconnu(s) du COG : %s. Codes valides : %s", ", ".join(unknown),
                      " ".join(sorted(known)))
            return None
    return depts


def load_cog_for_validation(args, root: Path) -> list[dict] | None:
    """COG à jour si l'étape inventory tourne en ligne ; sinon cache local s'il existe."""
    if "inventory" in args.steps and not args.offline:
        return inventory.ensure_cog(root, refresh=args.refresh_cog, cached_only=args.use_cached_cog)
    cache = root / "data" / "v_commune_2026.csv"
    return inventory.read_cog(cache.read_bytes().decode("utf-8-sig")) if cache.exists() else None


# ----------------------------------------------------------------- questions au lancement
def _last_file(root: Path) -> Path:
    return root / "data" / "dernier_lancement.json"


def load_last_depts(root: Path) -> list[str]:
    try:
        return [str(d) for d in json.loads(_last_file(root).read_text(encoding="utf-8")).get("departements", [])]
    except (OSError, ValueError, AttributeError):
        return []


def save_last_depts(root: Path, depts: list[str]) -> None:
    try:
        _last_file(root).parent.mkdir(parents=True, exist_ok=True)
        _last_file(root).write_text(json.dumps({"departements": depts}), encoding="utf-8")
    except OSError as exc:
        log.warning("Impossible de mémoriser les départements : %s", exc)


def _ask(input_fn, prompt: str) -> str | None:
    try:
        return input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def ask_yes_no(input_fn, prompt: str, default: bool = True) -> bool | None:
    suffix = "[O/n]" if default else "[o/N]"
    for _ in range(5):
        raw = _ask(input_fn, f"{prompt} {suffix} : ")
        if raw is None:
            return None
        if not raw:
            return default
        if raw.lower() in ("o", "oui", "y", "yes"):
            return True
        if raw.lower() in ("n", "non", "no"):
            return False
    return default


def ask_departments(args, known: set[str] | None, input_fn) -> list[str] | None:
    """Demande les départements au lancement. Retourne None si l'utilisateur renonce."""
    last = load_last_depts(args.root)
    default = " ".join(last)
    print("\nDépartement(s) à traiter : code(s) séparés par des espaces (ex : 60   ou   60 80 2A),")
    print("« tous » pour la France entière (très long).")
    if known:
        print(f"Codes valides : {' '.join(sorted(known))}")
    for _ in range(5):
        raw = _ask(input_fn, f"Département(s){f' [{default}]' if default else ''} : ")
        if raw is None:
            return None
        raw = raw or default
        if not raw:
            print("  Saisie vide : indiquez au moins un département.")
            continue
        parts = [p for p in re.split(r"[\s,;]+", raw) if p]
        if [p.lower() for p in parts] in (["tous"], ["all"]):
            if known is None:
                print("  « tous » nécessite le COG : lancez d'abord un département pour le télécharger.")
                continue
            if not ask_yes_no(input_fn, f"Confirmer l'exploration des {len(known)} départements ?", default=False):
                continue
            args.confirm_all = True
            return ["all"]
        depts = list(dict.fromkeys(normalize_dept(p) for p in parts))
        unknown = [d for d in depts if known is not None and d not in known]
        if unknown:
            print(f"  Département(s) inconnu(s) : {' '.join(unknown)}. Réessayez.")
            continue
        return depts
    print("Trop de saisies invalides : arrêt.")
    return None


def ask_contact(args, input_fn) -> None:
    """Demande l'e-mail de contact (facultatif) tant qu'il n'est pas renseigné ; mémorisé dans config.toml."""
    if args.contact or not getattr(args, "ask_contact", True):
        return
    print("\nE-mail ou URL de contact (facultatif) : ajouté au User-Agent, il permet à un webmestre de vous joindre")
    print("si votre robot le dérange. Laissez vide pour ne pas le renseigner.")
    raw = _ask(input_fn, "Contact : ")
    if raw:
        args.contact = raw
        try:
            cfg.update_value(args.config_path, "contact", raw)
            print(f"  Mémorisé dans {args.config_path}")
        except OSError as exc:
            log.warning("config.toml non modifiable : %s", exc)
    elif raw == "":
        try:
            cfg.update_value(args.config_path, "demander_contact", False)
            print("  D'accord, la question ne sera plus posée (modifiable dans config.toml).")
        except OSError:
            pass


def ask_resume(args, depts: list[str], input_fn) -> None:
    existing = [d for d in depts if DeptPaths(args.root, d).url_checks.exists()
                or DeptPaths(args.root, d).links_found.exists()]
    if not existing or args.resume:
        return
    print(f"\nDes résultats existent déjà pour : {' '.join(existing)}.")
    print("  [1] Reprendre : ne refait que ce qui manque ou a échoué (rapide)")
    print("  [2] Tout refaire : nouvelle recherche complète")
    raw = _ask(input_fn, "Choix [1] : ")
    args.resume = raw in (None, "", "1")


def confirm_run(args, depts: list[str], input_fn) -> bool:
    steps = [s for s in args.steps if not (s == "discover" and args.no_discover)]
    print("\n--- Récapitulatif ---")
    print(f"Départements : {' '.join(depts)}")
    print(f"Étapes       : {' → '.join(steps)}{'  (reprise)' if args.resume else ''}")
    print(f"Export       : {'une page par commune' if args.export_mode == 'une_page_par_commune' else 'toutes les pages'}"
          f", séparateur « {args.delimiter} »")
    procs = args.processes or auto_processes()
    per_proc = min(-(-args.concurrency // procs), args.max_par_processus)
    print(f"Performance  : {per_proc * procs} sites en parallèle ({procs} processus × {per_proc} tâches), "
          f"1 requête / {args.host_delay:g} s par site, robots.txt respecté")
    print(f"Réglages     : {args.config_path} (modifiables)")
    ok = ask_yes_no(input_fn, "Lancer ?", default=True)
    return bool(ok)


def ensure_dept(args, known: set[str] | None) -> bool:
    """Si --dept est absent : demande au lancement (session interactive) ; sinon erreur explicite."""
    if args.dept:
        return True
    if not args.interactive:
        log.error("Aucun département indiqué : ajoutez --dept 60 (ou lancez le programme sans argument pour le choisir).")
        return False
    chosen = ask_departments(args, known, args.input_fn)
    if chosen is None:
        return False
    args.dept = chosen
    args.asked = True
    return True


# ----------------------------------------------------------------- commandes
def auto_processes() -> int:
    """Un processus d'exploration par cœur utile, en laissant un cœur au processus principal (collecte, écriture)."""
    return max(1, min(6, (os.cpu_count() or 1) - 1))


def cmd_run(args) -> int:
    root = args.root
    csvio.set_delimiter(args.delimiter)
    args.steps = [s for s in cfg.STEPS if s in args.steps]  # ordre logique, sans doublon
    args.processes = args.processes or auto_processes()
    try:
        cog_rows = load_cog_for_validation(args, root)
    except Exception as exc:  # noqa: BLE001 - message clair, code retour 1
        log.error("COG indisponible : %s", exc)
        log.error("Astuce : si l'INSEE répond en erreur, relancez plus tard (ou --use-cached-cog avec un COG déjà téléchargé).")
        return 1
    known = set(inventory.cog_departments(cog_rows)) if cog_rows else None
    if not ensure_dept(args, known):
        return 2
    depts = resolve_depts(args, known)
    if depts is None:
        return 2
    if getattr(args, "asked", False):                       # session interactive : questions complémentaires
        ask_contact(args, args.input_fn)
        ask_resume(args, depts, args.input_fn)
        if not confirm_run(args, depts, args.input_fn):
            print("Annulé.")
            return 0
    elif not args.contact:
        log.warning("Aucun e-mail de contact (config.toml, clé « contact ») : recommandé pour identifier votre robot.")
    save_last_depts(root, [d for d in depts] if len(depts) < 20 else ["all"])
    log.info("Départements : %s ; étapes : %s ; %d processus, %d sites en parallèle", " ".join(depts),
             " ".join(args.steps), args.processes, args.concurrency)
    failures: list[str] = []
    active = [DeptPaths(root, d) for d in depts]

    # Les étapes réseau (discover, crawl) traitent TOUS les départements dans un seul pool de requêtes.
    if "inventory" in args.steps:
        ok = []
        for paths in active:
            print(f"\n=== Inventaire du département {paths.dept} ===")
            rc = inventory.run(paths, offline=args.offline, use_cached_cog=args.use_cached_cog, cog_rows=cog_rows)
            if rc != 0:
                failures.append(f"{paths.dept}/inventory")
                log.error("[%s] inventaire en échec : département ignoré pour la suite.", paths.dept)
            else:
                ok.append(paths)
        active = ok
    if "verify" in args.steps and active:
        print("\n=== Vérification des URL (diagnostic facultatif) ===")
        rc = verify_urls.run_many(active, Namespace(
            limit=args.limit_urls, concurrency=min(args.concurrency, 100), host_delay=args.host_delay,
            timeout=args.timeout, max_redirects=args.max_redirects, contact=args.contact,
            resume=args.resume, reclassify=False))
        if rc != 0:
            failures.append("verify")
    if "discover" in args.steps and active and not args.no_discover:
        print("\n=== Recherche des sites des communes sans site référencé ===")
        rc = discover.run_many(active, Namespace(resume=args.resume, concurrency=min(args.concurrency, 200),
                                                 timeout=args.timeout, contact=args.contact))
        if rc != 0:
            failures.append("discover")
    exported: set[str] = set()

    def export_dept(paths: DeptPaths) -> None:
        """Appelé dès qu'un département est terminé : écrit son CSV de résultat (puis le cumul)."""
        try:
            results.export_dept(paths, False, args.delimiter, args.export_mode)
            results.export_all(root, False, args.delimiter, args.export_mode)
            exported.add(paths.dept)
        except FileNotFoundError as exc:
            log.error("%s", exc)

    if "crawl" in args.steps and active:
        print("\n=== Exploration des sites (pool global, arrêt au premier lien) ===")

        def dept_done(paths: DeptPaths) -> None:
            coverage.report(paths)
            if "export" in args.steps:
                export_dept(paths)
        rc = crawl_links.run_many(active, Namespace(
            max_pages=args.max_pages, max_depth=args.max_depth, concurrency=args.concurrency,
            processes=args.processes, host_delay=args.host_delay, timeout=args.timeout, site_timeout=args.site_timeout,
            include=args.include, no_insecure_ssl=args.no_insecure_ssl, limit_sites=args.limit_sites,
            commune=args.commune, contact=args.contact, resume=args.resume, use_sitemap=not args.no_sitemap,
            recheck_known=not args.no_recheck_known, retry_failed=not args.no_retry,
            max_par_domaine=args.max_par_domaine, max_par_processus=args.max_par_processus,
            dns_simultanees=args.dns_simultanees, metrics=not args.no_metrics,
            adaptive_delay=getattr(args, "adaptive_delay", True), delai_max=getattr(args, "delai_max", 10.0),
            requetes_par_ip_max=getattr(args, "requetes_par_ip_max", 4)),
            on_dept_done=dept_done)
        if rc != 0:
            failures.append("crawl")
    if "plateformes" in args.steps and active:
        print("\n=== Pages PanneauPocket des communes ===")
        if platforms.run_many(active, Namespace(host_delay=args.host_delay, timeout=args.timeout, contact=args.contact,
                                                pages_panneaupocket=args.pages_panneaupocket)) != 0:
            failures.append("plateformes")
        for paths in active:
            coverage.report(paths)
        exported.clear()                                   # les liens trouvés sur PanneauPocket s'ajoutent : export refait
    if "export" in args.steps:
        for paths in active:
            if paths.dept not in exported:
                print("\n=== Export ===")
                export_dept(paths)
                if paths.dept not in exported:
                    failures.append(f"{paths.dept}/export")
    if {"crawl", "plateformes"} & set(args.steps):            # lot 4 et 5 : historique et compte rendu
        db = history.db_path(root)
        for paths in active:
            if paths.inventory_communes.exists():
                history.record(paths, db)
                print(f"[{paths.dept}] Historique : {history.summary_line(history.changes(db, paths.dept))}")
                print(f"[{paths.dept}] Compte rendu : {report.write(paths, db)}")
    if failures:
        log.error("Échecs : %s", ", ".join(failures))
        return 1
    return 0


def cmd_export(args) -> int:
    csvio.set_delimiter(args.delimiter)
    cache = args.root / "data" / "v_commune_2026.csv"
    known = set(inventory.cog_departments(inventory.read_cog(cache.read_bytes().decode("utf-8-sig")))) \
        if cache.exists() else None
    if not ensure_dept(args, known):
        return 2
    text = False
    if [d.lower() for d in args.dept] in (["all"], ["tous"]):
        return 0 if results.export_all(args.root, text, args.delimiter, args.export_mode) else 1
    depts = resolve_depts(args, known)
    if depts is None:
        return 2
    rc = 0
    for d in depts:
        try:
            results.export_dept(DeptPaths(args.root, d), text, args.delimiter, args.export_mode)
        except FileNotFoundError as exc:
            log.error("%s", exc)
            rc = 1
    results.export_all(args.root, text, args.delimiter, args.export_mode)
    return rc


def cmd_rapport(args) -> int:
    if not ensure_dept(args, None) or (depts := resolve_depts(args, None)) is None:
        return 2
    for d in depts:
        print(f"Compte rendu : {report.write(DeptPaths(args.root, d), history.db_path(args.root))}")
    return 0


def cmd_historique(args) -> int:
    if not ensure_dept(args, None) or (depts := resolve_depts(args, None)) is None:
        return 2
    db = history.db_path(args.root)
    for d in depts:
        ch = history.changes(db, d)
        print(f"[{d}] {history.summary_line(ch)}")
        for key, label in (("nouveaux", "nouveau"), ("disparus_confirmes", "disparu (confirmé)"),
                           ("disparus_a_verifier", "non retrouvé (à vérifier)"), ("deplaces", "déplacé")):
            for r in ch[key]:
                print(f"  {label:26s} {r['code_insee']} {r['nom_commune']} {r['page']}")
    return 0


def cmd_publier(args) -> int:
    """Publie les résultats locaux (reports/, resultats/) dans la base Postgres (lecture seule côté interface)."""
    import store_pg
    from publications import FileSource
    if not args.database_url:
        log.error("Aucune base indiquée : option --database-url ou variable d'environnement DATABASE_URL")
        return 2
    try:
        conn = store_pg.connect(args.database_url)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    try:
        store_pg.ensure_schema(conn)
        res = store_pg.publish(FileSource(args.root), conn)
    finally:
        conn.close()
    print(f"Publié : {', '.join(res['departements']) or '(aucun département)'} ; "
          f"CSV global : {'oui' if res['csv_global'] else 'non'}")
    return 0


def cmd_web(args) -> int:
    import webapp
    try:
        server = webapp.serve(args.root, args.web_host, args.web_port, args.web_password)
    except (ValueError, OSError) as exc:
        log.error("%s", exc)
        return 2
    shown = "localhost" if args.web_host in webapp.LOCAL_HOSTS else "<nom-ou-adresse-de-ce-poste>"
    print(f"Application web : http://{shown}:{args.web_port}/  (Ctrl+C pour arrêter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_coverage(args) -> int:
    csvio.set_delimiter(getattr(args, "delimiter", ";"))
    if not ensure_dept(args, None):
        return 2
    depts = resolve_depts(args, None)
    if depts is None:
        return 2
    for d in depts:
        if not coverage.report(DeptPaths(args.root, d)):
            log.error("[%s] inventaire absent : lancez d'abord l'étape inventory.", d)
            return 1
    return 0


def cmd_tune(args) -> int:
    """Analyse un fichier de mesures (le plus récent par défaut) et recommande un réglage du parallélisme."""
    path = args.file
    if path is None:
        found = sorted((args.root / "data" / "performance").glob("perf-*.csv"))
        if not found:
            log.error("Aucune mesure : lancez d'abord une exploration (data/performance/perf-*.csv).")
            return 1
        path = found[-1]
    return tuning.report(Path(path))


def cmd_compare(args) -> int:
    csvio.set_delimiter(getattr(args, "delimiter", ";"))
    if not ensure_dept(args, None):
        return 2
    depts = resolve_depts(args, None)
    if depts is None:
        return 2
    return max(diagnostic.report(DeptPaths(args.root, d), [Path(p) for p in args.ancien]) for d in depts)


def cmd_reclassify(args) -> int:
    csvio.set_delimiter(getattr(args, "delimiter", ";"))
    if not ensure_dept(args, None):
        return 2
    depts = resolve_depts(args, None)
    if depts is None:
        return 2
    return max(verify_urls.reclassify(DeptPaths(args.root, d)) for d in depts)


def cmd_migrate_legacy(args) -> int:
    """Copie les fichiers de l'ancienne version (Oise seule) vers la nouvelle arborescence, sans rien supprimer."""
    paths = DeptPaths(args.root, normalize_dept(args.dept))
    source = (args.source or args.root).resolve()
    pairs = [(s, d) for s, d in legacy_map(paths, source) if s.resolve() != d.resolve()]   # source == destination : rien à copier
    found = [(s, d) for s, d in pairs if s.exists()]
    if not found:
        log.error("Aucun ancien fichier trouvé dans %s (cherché notamment : %s).", source, pairs[-1][0])
        log.error("Si vos anciens résultats sont dans un autre dossier, indiquez-le : "
                  "migrate-legacy --dept %s --source \"C:\\chemin\\vers\\ancien-dossier\"", paths.dept)
        return 1
    copied = 0
    for src, dst in found:
        if dst.exists() and not args.force:
            print(f"  conservé (existe déjà) : {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
        print(f"  {src} -> {dst}")
    if not paths.cog_all_types.exists() and paths.cog_communes.exists():
        shutil.copy2(paths.cog_communes, paths.cog_all_types)
        print(f"  (cog_all_types reconstitué depuis {paths.cog_communes.name})")
    print(f"{copied} fichier(s) copié(s) depuis {source}. Les anciens fichiers sont conservés.")
    return 0


# ----------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mairie_link_monitor", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="dossier racine du projet")
    ap.add_argument("--config", type=Path, default=None, help="fichier de configuration (défaut : config.toml à la racine)")
    sub = ap.add_subparsers(dest="command")

    def dept_arg(p):
        p.add_argument("--dept", nargs="+", default=None, metavar="CODE",
                       help="département(s) : 60, 02, 2A, 971… ou 'all' (avec --confirm-all). Absent : demandé au lancement.")
        p.add_argument("--confirm-all", action="store_true", help="confirme l'exploration de tous les départements")

    def export_args(p):
        p.add_argument("--export-mode", choices=cfg.EXPORT_MODES, default="une_page_par_commune",
                       help="CSV final : une page par commune (défaut) ou toutes les pages")
        p.add_argument("--delimiter", default=";", help="séparateur des CSV (';' pour Excel FR, ',' sinon)")

    r = sub.add_parser("run", help="exécute tout ou partie du pipeline")
    dept_arg(r)
    r.add_argument("--steps", nargs="+", choices=cfg.STEPS, default=list(cfg.DEFAULT_STEPS),
                   help="étapes à exécuter (ordre logique conservé)")
    r.add_argument("--resume", action="store_true", help="reprise : ne refait que le travail manquant ou transitoire")
    r.add_argument("--contact", default="", help="e-mail/URL de contact ajouté au User-Agent (recommandé)")
    g = r.add_argument_group("inventaire")
    g.add_argument("--offline", action="store_true", help="réutilise les CSV de data/<dep>/ sans réseau")
    g.add_argument("--use-cached-cog", action="store_true", help="n'utilise que le COG déjà téléchargé")
    g.add_argument("--refresh-cog", action="store_true", help="retélécharge le COG")
    g = r.add_argument_group("performance et politesse")
    g.add_argument("--concurrency", type=int, default=300, help="sites explorés en parallèle (tâches asyncio, total)")
    g.add_argument("--processes", type=int, default=0, help="processus d'exploration (0 = automatique selon le processeur)")
    g.add_argument("--no-metrics", action="store_true", help="n'enregistre pas les mesures de performance")
    g.add_argument("--max-par-processus", type=int, default=100, help="plafond de tâches asyncio par processus")
    g.add_argument("--dns-simultanees", type=int, default=48, help="résolutions DNS simultanées au maximum (tous processus)")
    g.add_argument("--page-panneaupocket", dest="pages_panneaupocket", action="append", default=[],
                   help="page PanneauPocket à lire en plus (répétable)")
    g.add_argument("--max-par-domaine", type=int, default=4, help="sites simultanés max sur un même domaine d'hébergement")
    g.add_argument("--host-delay", type=float, default=0.5, help="délai minimal (s) entre requêtes vers un même site")
    g.add_argument("--delai-fixe", dest="adaptive_delay", action="store_false",
                   help="délai fixe par site (--host-delay) : pas d'adaptation au temps de réponse ni aux erreurs")
    g.add_argument("--delai-adaptatif", dest="adaptive_delay", action="store_true", help="délai adaptatif (défaut)")
    g.add_argument("--delai-max", type=float, default=10.0, help="délai maximal (s) par site en mode adaptatif")
    g.add_argument("--requetes-par-ip-max", type=int, default=4,
                   help="requêtes simultanées au maximum vers une même adresse IP (hébergeurs mutualisés)")
    g.add_argument("--timeout", type=float, default=15.0)
    g.add_argument("--max-redirects", type=int, default=5, help="(étape verify)")
    g.add_argument("--limit-urls", type=int, default=0, help="(étape verify) N premières URL seulement (essai)")
    g = r.add_argument_group("exploration (arrêt au premier lien trouvé)")
    g.add_argument("--max-pages", type=int, default=40)
    g.add_argument("--max-depth", type=int, default=2)
    g.add_argument("--site-timeout", type=float, default=180.0, help="durée max (s) par site")
    g.add_argument("--include", nargs="+", default=list(crawl_links.DEFAULT_INCLUDE), help="niveaux de confiance à explorer")
    g.add_argument("--no-recheck-known", action="store_true",
                   help="ne revérifie pas en premier la page où un lien avait déjà été trouvé")
    g.add_argument("--no-retry", action="store_true", help="ne retente pas les sites en échec passager")
    g.add_argument("--no-discover", action="store_true", help="ne cherche pas les sites des communes sans site référencé")
    g.add_argument("--use-sitemap", dest="no_sitemap", action="store_false", help="ajoute les premières pages de sitemap.xml")
    g.add_argument("--no-sitemap", dest="no_sitemap", action="store_true", help="n'utilise pas sitemap.xml (défaut)")
    r.set_defaults(no_sitemap=True)
    g.add_argument("--no-insecure-ssl", action="store_true", help="n'explore pas les sites à certificat invalide")
    g.add_argument("--limit-sites", type=int, default=0, help="N sites seulement (essai)")
    g.add_argument("--commune", action="append", help="code INSEE à explorer (répétable)")
    export_args(r)
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("export", help="régénère le CSV minimal (code INSEE, URL de la page) sans réseau")
    dept_arg(e)
    export_args(e)
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("reclassify", help="réapplique les heuristiques de confiance (sans réseau)")
    dept_arg(c)
    c.set_defaults(func=cmd_reclassify)

    cv = sub.add_parser("coverage", help="affiche combien de communes sont analysées et pourquoi les autres ne le sont pas")
    dept_arg(cv)
    cv.set_defaults(func=cmd_coverage)

    w = sub.add_parser("web", help="application web pour lancer les analyses et consulter les résultats")
    w.add_argument("--host", dest="web_host", default="127.0.0.1", help="127.0.0.1 = ce poste ; 0.0.0.0 = réseau")
    w.add_argument("--port", dest="web_port", type=int, default=8080)
    w.add_argument("--password", dest="web_password", default="", help="mot de passe (sinon config.toml)")
    w.set_defaults(func=cmd_web)

    rp = sub.add_parser("rapport", help="écrit le compte rendu HTML du département (resultats/rapport_<dep>.html)")
    dept_arg(rp)
    rp.set_defaults(func=cmd_rapport)

    hi = sub.add_parser("historique", help="affiche les liens nouveaux et disparus depuis le contrôle précédent")
    dept_arg(hi)
    hi.set_defaults(func=cmd_historique)

    pu = sub.add_parser("publier", help="publie les résultats dans la base Postgres (interface web Vercel)")
    pu.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                    help="URL Postgres (défaut : variable d'environnement DATABASE_URL)")
    pu.set_defaults(func=cmd_publier)

    tn = sub.add_parser("tune", help="analyse les mesures de performance et recommande un réglage du parallélisme")
    tn.add_argument("--file", type=Path, default=None, help="fichier de mesures (défaut : le plus récent)")
    tn.set_defaults(func=cmd_tune)

    cp = sub.add_parser("compare", help="compare avec d'anciens résultats et explique les communes perdues")
    dept_arg(cp)
    cp.add_argument("--ancien", nargs="+", required=True, metavar="FICHIER",
                    help="ancien(s) fichier(s) de liens (ex. ...\\reports\\links_found_oise.csv)")
    cp.set_defaults(func=cmd_compare)

    m = sub.add_parser("migrate-legacy", help="copie les fichiers de l'ancienne version (Oise) vers la nouvelle arborescence")
    m.add_argument("--dept", default="60")
    m.add_argument("--source", type=Path, default=None, help="ancien dossier de projet (par défaut : le dossier courant)")
    m.add_argument("--force", action="store_true", help="écrase les fichiers déjà migrés")
    m.set_defaults(func=cmd_migrate_legacy)
    return ap


def parse_args(argv: list[str]):
    """Analyse la ligne de commande ; config.toml fournit les valeurs par défaut (la ligne de commande l'emporte)."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--root", type=Path, default=ROOT)
    pre.add_argument("--config", type=Path, default=None)
    known, _ = pre.parse_known_args(argv)
    config_path = known.config or (known.root / "config.toml")
    parser = build_parser()
    if (not config_path.exists() and known.config is None and any(a in argv for a in ("run", "export", "web"))
            and not any(a in ("-h", "--help") for a in argv)):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(cfg.template(), encoding="utf-8")
        print(f"Fichier de réglages créé : {config_path}  (modifiable ; la ligne de commande reste prioritaire)")
    if config_path.exists() and known.config is None:
        message = cfg.migrate(config_path)
        if message:
            print(message)
    values = cfg.load(config_path) if config_path.exists() else cfg.defaults()
    sub = parser._subparsers._group_actions[0].choices          # noqa: SLF001 - sous-parsers argparse
    for name in ("run", "export", "web"):
        dests = {a.dest for a in sub[name]._actions}            # noqa: SLF001
        sub[name].set_defaults(**{d: v for d, v in values.items() if d in dests})
    args = parser.parse_args(argv)
    args.config_path = config_path
    args.ask_contact = values.get("ask_contact", True)
    return args


def main(argv: list[str] | None = None, input_fn=None) -> int:
    """input_fn : fonction de saisie (injectable pour les tests) ; par défaut `input` si le terminal est interactif."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in COMMANDS for a in argv) and not any(a in ("-h", "--help") for a in argv):
        i = 0                                                   # sans commande : lancement « run » (interactif)
        while i + 1 < len(argv) and argv[i] in ("--root", "--config"):
            i += 2
        argv.insert(i, "run")
    try:
        args = parse_args(argv)
    except cfg.ConfigError as exc:
        log.error("%s", exc)
        return 2
    args.input_fn = input_fn or input
    args.interactive = input_fn is not None or sys.stdin.isatty()
    if getattr(args, "func", None) is None:
        build_parser().print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
