"""Configuration par défaut (config.toml à la racine du projet). La ligne de commande reste prioritaire.

Le département (--dept) et les communes (--commune) ne se configurent PAS ici : le département se choisit à chaque
lancement du programme (question posée au démarrage).

Chaque clé du fichier est formulée « positivement » (true = la fonction est active). `invert` indique que l'option
interne correspondante est négative (ex. clé `utiliser_sitemap` <-> option `--no-sitemap`).
"""
from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_VERSION = 4
STEPS = ("inventory", "verify", "discover", "crawl", "plateformes", "export")
DEFAULT_STEPS = ("inventory", "discover", "crawl", "plateformes", "export")     # verify : diagnostic facultatif, non nécessaire
EXPORT_MODES = ("une_page_par_commune", "toutes_les_pages")


@dataclass(frozen=True)
class Option:
    key: str            # nom dans config.toml
    dest: str           # nom de l'option interne (argparse)
    default: object     # valeur PAR DÉFAUT DANS LE FICHIER (sens positif)
    comment: str
    invert: bool = False


OPTIONS: list[Option] = [
    Option("version_config", "config_version", CONFIG_VERSION, "Version du format de ce fichier (ne pas modifier)."),
    Option("contact", "contact", "", "Votre e-mail ou URL de contact : ajouté au User-Agent des requêtes (recommandé)."),
    Option("demander_contact", "ask_contact", True,
           "true = le programme demande l'e-mail de contact au lancement tant qu'il n'est pas renseigné."),
    Option("etapes", "steps", list(DEFAULT_STEPS), "Étapes exécutées : inventory, verify, discover, crawl, plateformes, export."),
    Option("reprise", "resume", False, "true = ne refait que le travail manquant ou en échec transitoire."),
    Option("parallelisme", "concurrency", 300,
           "Nombre total de sites explorés en parallèle (tâches asyncio, tous processus confondus). Le délai par site, "
           "robots.txt et le plafond par domaine restent appliqués."),
    Option("processus", "processes", 0,
           "Nombre de processus d'exploration (un par cœur utile). 0 = automatique selon le processeur."),
    Option("parallelisme_max_par_processus", "max_par_processus", 100,
           "Plafond de tâches asyncio par processus : au-delà (mesuré), le coût CPU par page augmente et le débit baisse."),
    Option("enregistrer_mesures_performance", "no_metrics", True,
           "true = enregistre débit, taux de réussite, latences, CPU et saturation (data/performance/*.csv) et affiche une "
           "recommandation de réglage en fin d'exploration.", invert=True),
    Option("resolutions_dns_simultanees", "dns_simultanees", 48,
           "Résolutions DNS simultanées au maximum (tous processus). Trop haut, le serveur DNS de la box sature et des sites "
           "accessibles sont notés « erreur_dns » ; chaque échec est retenté avec une pause croissante."),
    Option("pages_panneaupocket", "pages_panneaupocket", [],
           "Pages PanneauPocket à lire en plus de celles trouvées automatiquement "
           "(ex. \"https://app.panneaupocket.com/ville/325595414-sery-magneval-60800\")."),
    Option("max_sites_par_domaine", "max_par_domaine", 4,
           "Plafond de sites explorés simultanément sur un même domaine d'hébergement (ex. e-monsite.com)."),
    Option("delai_par_site_s", "host_delay", 1.0,
           "Délai minimal (secondes) entre deux requêtes vers un même site (politesse ; Crawl-delay de robots.txt prioritaire)."),
    Option("timeout_s", "timeout", 15.0, "Délai maximal (secondes) d'une requête."),
    Option("redirections_max", "max_redirects", 5, "Nombre maximal de redirections suivies."),
    Option("niveaux_confiance_a_explorer", "include",
           ["probable_officiel", "a_verifier", "plateforme_tierce_a_verifier", "intercommunal_ou_service_probable",
            "tiers_probable", "devine_a_verifier"],
           "Niveaux de confiance des sites à explorer : par défaut TOUS (sauf institutions non communales)."),
    Option("explorer_sites_certificat_invalide", "no_insecure_ssl", True,
           "true = explore aussi les sites à certificat invalide (signalés ssl_invalide).", invert=True),
    Option("pages_max_par_site", "max_pages", 40, "Pages maximum lues par site."),
    Option("profondeur_max", "max_depth", 2, "Profondeur maximale depuis la page d'accueil."),
    Option("duree_max_par_site_s", "site_timeout", 180.0, "Durée maximale (secondes) par site. Pages lues au plus = durée ÷ delai_par_site_s (180 s ÷ 1,3 s ≈ 138 pages), quelle que soit pages_max_par_site."),
    Option("utiliser_sitemap", "no_sitemap", False,
           "true = ajoute les premières pages de sitemap.xml à l'exploration (dans l'ordre du fichier).", invert=True),
    Option("reverifier_page_connue", "no_recheck_known", True,
           "true = relance : revérifie d'abord la page où le lien avait été trouvé.", invert=True),
    Option("nouvelle_tentative_sites_en_echec", "no_retry", True,
           "true = un site en échec passager (timeout, connexion, 5xx) est retenté une fois, plus lentement.", invert=True),
    Option("decouvrir_sites_manquants", "no_discover", True,
           "true = pour les communes sans site référencé dans la DILA, cherche leur site par noms de domaine usuels "
           "(résultats signalés « devine_a_verifier » ; validés par le nom de la commune, « mairie » et un code postal).",
           invert=True),
    Option("adresse_web", "web_host", "127.0.0.1",
           "Application web : « 127.0.0.1 » = ce poste seulement ; « 0.0.0.0 » = accessible aux collègues du réseau "
           "(mot de passe obligatoire)."),
    Option("port_web", "web_port", 8080, "Application web : port (adresse http://<nom-du-poste>:8080)."),
    Option("mot_de_passe_web", "web_password", "", "Application web : mot de passe demandé aux utilisateurs."),
    Option("export_mode", "export_mode", "une_page_par_commune",
           "CSV final : « une_page_par_commune » (recommandé) ou « toutes_les_pages »."),
    Option("separateur_csv", "delimiter", ";",
           "Séparateur de tous les fichiers CSV : « ; » (Excel français) ou « , »."),
]
BY_KEY = {o.key: o for o in OPTIONS}
PRESERVED_ON_MIGRATION = ("contact", "demander_contact", "separateur_csv", "export_mode")


class ConfigError(Exception):
    pass


def internal_default(o: Option):
    """Valeur par défaut de l'option interne (sens argparse)."""
    return (not o.default) if o.invert else o.default


def _render(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, list)):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def template(preserved: dict | None = None) -> str:
    preserved = preserved or {}
    lines = ["# Configuration de mairie-link-monitor.",
             "# Les options de la ligne de commande ont priorité sur ce fichier.",
             "# Le département se choisit à chaque lancement du programme, pas ici.", ""]
    for o in OPTIONS:
        value = preserved.get(o.key, o.default)
        lines += [f"# {o.comment}", f"{o.key} = {_render(value)}", ""]
    return "\n".join(lines)


def update_value(path: Path, key: str, value) -> None:
    """Remplace (ou ajoute) `key = value` dans le fichier, sans toucher au reste ni aux commentaires."""
    text = path.read_text(encoding="utf-8-sig")
    line = f"{key} = {_render(value)}"
    new, n = re.subn(rf"(?m)^{re.escape(key)}\s*=.*$", lambda _m: line, text)
    path.write_text(new if n else text.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")


def migrate(path: Path) -> str | None:
    """Fichier créé par une version précédente : sauvegarde en .bak puis mise à jour. Version < 3 (clés obsolètes) :
    régénération en conservant le contact et le format d'export. Version 3 : tous vos réglages sont conservés, seule
    l'étape « plateformes » est ajoutée. Retourne un message, ou None si rien à faire."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    if raw.get("version_config", 0) >= CONFIG_VERSION:
        return None
    backup = path.with_suffix(".toml.bak")
    shutil.copy2(path, backup)
    if raw.get("version_config", 0) < 3:
        path.write_text(template({k: raw[k] for k in PRESERVED_ON_MIGRATION if k in raw}), encoding="utf-8")
        return f"{path.name} mis à jour au nouveau format (ancienne version sauvegardée : {backup.name})"
    steps = raw.get("etapes")
    if isinstance(steps, list) and "plateformes" not in steps:
        update_value(path, "etapes", [s for s in steps if s != "export"] + ["plateformes"] + (["export"] if "export" in steps else []))
    update_value(path, "version_config", CONFIG_VERSION)
    return f"{path.name} mis à jour au nouveau format (ancienne version sauvegardée : {backup.name})"


def load(path: Path) -> dict:
    """Retourne {dest: valeur interne} validé. ConfigError avec un message clair en cas de problème."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} : erreur de syntaxe TOML ({exc})") from exc
    out: dict = {}
    for key, value in raw.items():
        o = BY_KEY.get(key)
        if o is None:
            raise ConfigError(f"{path} : option inconnue « {key} ». Options valides : {', '.join(BY_KEY)}")
        expected = type(o.default)
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigError(f"{path} : « {key} » doit être de type {expected.__name__} (reçu : {value!r})")
        if o.dest == "steps":
            bad = [s for s in value if s not in STEPS]
            if bad:
                raise ConfigError(f"{path} : étapes inconnues {bad}. Valides : {', '.join(STEPS)}")
        if o.dest == "delimiter" and value not in (",", ";", "\t"):
            raise ConfigError(f"{path} : separateur_csv doit être \";\" ou \",\"")
        if o.dest == "export_mode" and value not in EXPORT_MODES:
            raise ConfigError(f"{path} : export_mode doit valoir {' ou '.join(EXPORT_MODES)}")
        if o.dest in ("concurrency", "processes", "max_par_domaine", "max_par_processus", "max_pages", "max_depth") and value < 0:
            raise ConfigError(f"{path} : « {key} » ne peut pas être négatif")
        out[o.dest] = (not value) if o.invert else value
    return out


def defaults() -> dict:
    return {o.dest: internal_default(o) for o in OPTIONS}
