"""Synthèse par département : statut de chaque commune pour le dernier contrôle, au format de l'ancien logiciel.

`synthese_departement` lit les fichiers du département (inventaire, exploration, plateformes, couverture) et le dernier
contrôle de l'historique ; `resume` en tire les compteurs de la page « Rapports ». Les libellés (causes d'échec et
limites d'exploration) sont ici aussi, pour que l'interface les importe depuis un seul module.

Règle de statut (première règle qui s'applique) :
  1. lien_confirme        : au moins un lien cliquable vers la cible (links_found.csv, liens_plateformes.csv)
  2. lien_incertain       : pas de lien, mais une ligne de crawl_sites.csv signale une URL du portail en texte brut
  3. aucun_lien_complet   : un site lu en entier sans lien (explore_complet)
  4. aucun_lien_partiel   : un site lu partiellement sans lien (explore_partiel)
  5. aucun_lien_complet   : page PanneauPocket lue, sans lien (même convention que l'historique)
  6. inaccessible         : aucun site lu, au moins un site en erreur (erreur_acces, robots_interdit, robots_indisponible)
  7. sans_site            : aucun site connu (couverture : « aucun site référencé… »)
  8. non_analysee         : un site ou une commune traitée, sans résultat d'exploration dans ce contrôle

`non_exploree` n'est plus produit : son compteur reste à 0 pour garder la forme de l'ancien logiciel.

Table des causes (statut du site -> clé de CAUSES), appliquée au champ motif puis au champ erreur :
  statut robots_interdit                         -> interdit_robots
  motif « erreur interne »                       -> erreur_interne
  « adresse_non_publique »                       -> adresse_non_publique
  « erreur_dns », « nom inexistant », « DNS : »  -> dns_introuvable
  « erreur_ssl », certificat, SSL                -> certificat_invalide
  « timeout », « durée maximale »                -> delai_depasse
  « http_5xx », « HTTP 5xx »                     -> serveur_en_erreur
  « bloque_ou_limite », anti-robot, HTTP 401/403/429 -> acces_refuse
  « erreur_connexion », ConnectError, ReadError, RemoteProtocolError -> connexion_impossible
  « http_4xx 404 »                               -> page_introuvable
  « trop_de_redirections », « redirection_externe » -> redirection_invalide
  « non_html »                                   -> contenu_invalide
  autre                                          -> None (libellé générique « Cause non précisée »)
Les sites lus (explore_*) et écartés (non_explore) n'ont pas de cause.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import coverage
import csvio
import history
import results
from departements import nom_departement
from layout import DeptPaths

STATUTS_COMMUNE = (
    "lien_confirme", "lien_incertain", "aucun_lien_complet", "aucun_lien_partiel",
    "inaccessible", "non_exploree", "sans_site", "non_analysee",
)
EXPLORES = ("explore_complet", "explore_partiel")
ERREURS = ("erreur_acces", "robots_interdit", "robots_indisponible")
ECARTE = "non_explore"  # site écarté avant exploration (confiance insuffisante)
DECOUVERTE = "decouverte"

# cause -> (titre court, explication en une phrase, conseil d'action) ; même clés et mêmes textes que l'ancien
# libelles.CAUSES, dans le même ordre (l'interface range les sections selon cet ordre).
CAUSES: dict[str, tuple[str, str, str]] = {
    "dns_introuvable": (
        "Adresse introuvable",
        "Le nom de domaine n'existe pas ou plus : le site est probablement fermé ou l'adresse de l'annuaire est erronée.",
        "Vérifier l'adresse du site dans l'annuaire Service-Public et la faire corriger."),
    "certificat_invalide": (
        "Certificat de sécurité invalide",
        "Le site présente un certificat de sécurité expiré, mal configuré ou non reconnu : la connexion sécurisée est impossible.",
        "Prévenir la mairie, puis consulter le site manuellement en connaissance de cause."),
    "delai_depasse": (
        "Site trop lent ou muet",
        "Le site n'a pas répondu dans le temps imparti, même après une seconde tentative.",
        "Réessayer plus tard ; si le problème persiste, consulter le site manuellement."),
    "serveur_en_erreur": (
        "Site en panne",
        "Le serveur du site a répondu par une erreur : il est probablement en panne ou en maintenance.",
        "Réessayer plus tard ; si le problème persiste, prévenir la mairie."),
    "acces_refuse": (
        "Protection anti-robot ou accès refusé",
        "Le site refuse l'accès aux outils automatiques ou les arrête par une protection anti-robot ; l'outil ne la contourne pas.",
        "Consulter le site manuellement."),
    "interdit_robots": (
        "Exploration interdite par le site (robots.txt)",
        "Le site demande explicitement aux outils automatiques de ne pas le parcourir, et l'outil respecte cette demande.",
        "Consulter le site manuellement."),
    "connexion_impossible": (
        "Connexion impossible",
        "Le serveur du site a refusé ou coupé la connexion.",
        "Réessayer plus tard ; si le problème persiste, consulter le site manuellement."),
    "adresse_non_publique": (
        "Adresse non publique",
        "L'adresse du site pointe vers un réseau interne ou réservé : l'outil refuse de s'y connecter par sécurité.",
        "Vérifier l'adresse du site dans l'annuaire Service-Public et la faire corriger."),
    "page_introuvable": (
        "Page d'accueil introuvable",
        "L'adresse indiquée dans l'annuaire répond « page inexistante » : le site a probablement changé d'adresse.",
        "Chercher la nouvelle adresse du site et la faire corriger dans l'annuaire Service-Public."),
    "redirection_invalide": (
        "Redirection en boucle ou refusée",
        "Le site renvoie vers une autre adresse en boucle, ou vers une adresse que l'outil refuse de suivre.",
        "Consulter le site manuellement."),
    "contenu_invalide": (
        "Contenu illisible",
        "L'adresse ne renvoie pas une page web exploitable (fichier, document ou page trop volumineuse).",
        "Vérifier l'adresse du site dans l'annuaire Service-Public."),
    "erreur_interne": (
        "Erreur de l'outil",
        "L'analyse de ce site a échoué à cause d'un problème interne à l'outil.",
        "Relancer l'analyse de ce département ; si cela se reproduit, consulter le journal."),
    # regroupements propres au compte rendu (communes non analysables sans cause d'échec)
    "plateforme_non_exploree": (
        "Plateforme partagée non explorée",
        "Le site est hébergé sur une plateforme commune à de nombreuses mairies : il n'est pas exploré par choix.",
        "Consulter le site manuellement."),
    "sans_resultat": (
        "Pas de résultat dans cette analyse",
        "Un site est connu pour cette commune, mais il n'a pas été analysé pendant cette recherche.",
        "Relancer l'analyse de ce département."),
    "inconnue": (
        "Cause non précisée",
        "Le site n'a pas pu être analysé, sans cause enregistrée.",
        "Consulter le site manuellement."),
}

# limite atteinte pendant l'exploration -> phrase (adaptées : « limite de pages » plutôt que « site très grand »)
LIMITES: dict[str, str] = {
    "max_pages_par_domaine": "limite de pages atteinte",
    "duree_site": "durée maximale par site atteinte",
    "profondeur_max": "limite de profondeur atteinte",
    "pages_tronquees": "page trop volumineuse, lue partiellement",
}
LIMITE_INCONNUE = "exploration interrompue avant la fin du site"

# (expression, cause) : testées dans l'ordre ; minuscules, appliquées au motif puis au champ erreur
_REGLES_CAUSE: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(p), c) for p, c in (
    (r"erreur interne", "erreur_interne"),
    (r"adresse_non_publique", "adresse_non_publique"),
    (r"erreur_dns|nom inexistant|dns :", "dns_introuvable"),
    (r"erreur_ssl|certificat|certificate|\bssl\b", "certificat_invalide"),
    (r"timeout|durée maximale", "delai_depasse"),
    (r"http_5xx|http 5\d\d", "serveur_en_erreur"),
    (r"bloque_ou_limite|anti-robot|http (?:401|403|429)", "acces_refuse"),
    (r"erreur_connexion|connecterror|readerror|remoteprotocolerror", "connexion_impossible"),
    (r"http_4xx 404", "page_introuvable"),
    (r"trop_de_redirections|redirection_externe", "redirection_invalide"),
    (r"non_html", "contenu_invalide"),
))


def libelle_cause(cause: str | None) -> dict[str, str]:
    """Libellé d'une cause ; tolérant : cause inconnue ou None -> libellé générique « Cause non précisée »."""
    titre, explication, conseil = CAUSES.get(cause or "", CAUSES["inconnue"])
    return {"cause": cause if cause in CAUSES else "inconnue", "titre": titre, "explication": explication,
            "conseil": conseil}


def libelle_limite(limite: str) -> str:
    return LIMITES.get(limite, LIMITE_INCONNUE)


def cause_site(statut: str, motif: str, erreur: str) -> str | None:
    """Cause d'un site non lu, d'après la table du module. Le motif est prioritaire sur le champ erreur."""
    if statut == "robots_interdit":
        return "interdit_robots"
    if statut not in ERREURS:
        return None
    for texte in (motif or "", erreur or ""):
        bas = texte.lower()
        for regle, cause in _REGLES_CAUSE:
            if regle.search(bas):
                return cause
    return None


def _sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn").casefold()


def _nb(n: int, singulier: str, pluriel: str) -> str:
    return f"{n} {singulier if n == 1 else pluriel}"


def _entier(valeur: Any) -> int:
    try:
        return int(valeur or 0)
    except (TypeError, ValueError):
        return 0


def _hote(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _liens(paths: DeptPaths) -> dict[str, list[dict]]:
    """Liens cliquables par commune, une entrée par ligne de links_found.csv / liens_plateformes.csv (doublons exacts
    supprimés). Mêmes filtres que results.collect."""
    sortie: dict[str, list[dict]] = defaultdict(list)
    vus: set[tuple] = set()
    for fichier in (paths.links_found, paths.platform_links):
        if not fichier.exists():
            continue
        for r in csvio.read_rows(fichier):
            type_lien = r.get("type_lien", "lien")
            code, page = (r.get("code_insee") or "").strip(), (r.get("page_url") or "").strip()
            if type_lien not in results.CLICKABLE_TYPES or not code or not page:
                continue
            entree = {"url_page_source": page, "url_cible": r.get("target_url", ""), "texte": r.get("texte_lien", ""),
                      "type_element": type_lien, "verification": None, "remarque": ""}
            cle = (code, page, entree["url_cible"], entree["texte"], type_lien)
            if cle not in vus:
                vus.add(cle)
                sortie[code].append(entree)
    return sortie


def _site(r: dict, decouvert: bool) -> dict:
    statut = r.get("statut_crawl", "")
    motif, erreur = r.get("motif", "") or "", r.get("erreur", "") or ""
    limites = []
    if _entier(r.get("limite_pages_atteinte")):
        limites.append("max_pages_par_domaine")
    if "durée maximale" in motif:
        limites.append("duree_site")
    if _entier(r.get("limite_profondeur_atteinte")):
        limites.append("profondeur_max")
    if _entier(r.get("pages_tronquees")):
        limites.append("pages_tronquees")
    return {
        "domaine": _hote(r.get("site_url", "")), "url_depart": r.get("site_url", ""), "statut": statut,
        "nb_pages": _entier(r.get("pages_visitees")), "limites": limites, "erreurs": [erreur] if erreur else [],
        "cause": cause_site(statut, motif, erreur), "tls_non_verifie": bool(_entier(r.get("ssl_invalide"))),
        "source": DECOUVERTE if decouvert else None, "a_verifier": decouvert,
    }


def _classer(sites: list[dict], nb_liens: int, incertain: bool, etat: str) -> str:
    """Statut d'une commune (règle de la docstring du module). `etat` vient de coverage.build."""
    if nb_liens:
        return "lien_confirme"
    if incertain:
        return "lien_incertain"
    explores = [s for s in sites if s["statut"] in EXPLORES]
    if explores:
        return "aucun_lien_complet" if all(s["statut"] == "explore_complet" for s in explores) else "aucun_lien_partiel"
    if etat.startswith("page PanneauPocket lue"):
        return "aucun_lien_complet"
    if any(s["statut"] in ERREURS for s in sites):
        return "inaccessible"
    if not sites and etat.startswith("aucun site"):
        return "sans_site"
    return "non_analysee"


def _detail(statut: str, sites: list[dict], nb_liens: int, nb_incertains: int) -> str:
    completes = sum(s["statut"] == "explore_complet" for s in sites)
    partiels = sum(s["statut"] == "explore_partiel" for s in sites)
    erreurs = sum(s["statut"] in ERREURS for s in sites)
    ecartes = sum(s["statut"] == ECARTE for s in sites)
    morceaux = []
    if statut == "lien_confirme":
        morceaux.append(_nb(nb_liens, "lien confirmé vers la cible", "liens confirmés vers la cible"))
    elif statut == "lien_incertain":
        morceaux.append("aucun lien confirmé ; " + _nb(nb_incertains, "élément incertain à vérifier",
                                                       "éléments incertains à vérifier"))
    elif statut == "aucun_lien_complet":
        if completes:
            morceaux.append(f"aucun lien ; exploration complète de {_nb(completes, 'site', 'sites')}")
        else:
            morceaux.append("aucun lien ; page PanneauPocket lue")
    elif statut == "aucun_lien_partiel":
        morceaux.append(f"aucun lien confirmé ; exploration incomplète de {_nb(partiels, 'site', 'sites')}")
    elif statut == "inaccessible":
        morceaux.append("aucun site exploré")
    elif statut == "non_analysee":
        morceaux.append("site identifié, sans résultat d'exploration" if sites
                        else "commune non traitée : exploration non exécutée")
    else:
        morceaux.append("aucun site officiel identifié")
    if erreurs and statut != "inaccessible":
        morceaux.append(_nb(erreurs, "site inaccessible ou bloqué, non exploré", "sites inaccessibles ou bloqués, non explorés"))
    if ecartes:
        morceaux.append(_nb(ecartes, "site écarté avant exploration (confiance insuffisante)",
                            "sites écartés avant exploration (confiance insuffisante)"))
    return " ; ".join(morceaux)


def synthese_departement(paths: DeptPaths, db: Path) -> dict | None:
    """Synthèse du dernier contrôle du département, au format de l'ancien logiciel. None sans inventaire."""
    if not paths.inventory_communes.exists():
        return None
    communes = csvio.read_rows(paths.inventory_communes)
    etats = {s["code_insee"]: s["etat"] for s in coverage.build(paths)}
    sites_par_commune: dict[str, list[dict]] = defaultdict(list)
    texte_brut: set[str] = set()     # communes dont une ligne de crawl signale une URL du portail en texte brut
    if paths.crawl_sites.exists():
        decouverts = set()
        if paths.discovered.exists():
            decouverts = {(r.get("code_insee", ""), r.get("url_candidate", "")) for r in csvio.read_rows(paths.discovered)}
        for r in csvio.read_rows(paths.crawl_sites):
            code = r.get("code_insee", "")
            sites_par_commune[code].append(_site(r, (code, r.get("site_url", "")) in decouverts))
            if "texte brut" in (r.get("motif") or ""):
                texte_brut.add(code)
    liens = _liens(paths)
    try:
        avec_lien = {r["code_insee"] for r in results.collect(paths, mode="toutes_les_pages")}
    except FileNotFoundError:
        avec_lien = set()

    ctl = history.controls(db, paths.dept)
    date = ctl[0]["date"] if ctl else ""

    totaux = dict.fromkeys(STATUTS_COMMUNE, 0)
    lignes = []
    for c in sorted(communes, key=lambda c: (_sans_accents(c["nom_commune"]), c["code_insee"])):
        code = c["code_insee"]
        sites = sites_par_commune.get(code, [])
        confirmes = liens.get(code, []) if code in avec_lien else []
        statut = _classer(sites, len(confirmes), code in texte_brut, etats.get(code, ""))
        totaux[statut] += 1
        nb_incertains = 1 if statut == "lien_incertain" else 0
        cause_principale = None
        if statut == "inaccessible":
            cause_principale = next((s["cause"] for s in sites if s["statut"] not in EXPLORES and s["cause"]), None)
        lignes.append({
            "code_insee": code, "nom": c["nom_commune"], "statut": statut,
            "detail": _detail(statut, sites, len(confirmes), nb_incertains),
            "sites": sites, "liens_confirmes": confirmes, "nb_liens_incertains": nb_incertains,
            "cause_principale": cause_principale,
            "site_a_verifier": any(s["a_verifier"] for s in sites),
        })

    return {
        "departement": {"code": paths.dept, "nom": nom_departement(paths.dept)},
        "campagne": {"id": None, "debut": date, "fin": date, "statut": "terminee"},
        "totaux": totaux,
        "communes": lignes,
    }


def resume(synth: dict) -> dict:
    """Compteurs d'un département, mêmes clés que `derniers_rapports` de l'ancien logiciel."""
    t = synth["totaux"]
    campagne = synth["campagne"]
    return {
        "code": synth["departement"]["code"], "nom": synth["departement"]["nom"],
        "date": campagne.get("fin") or campagne.get("debut", ""), "statut_campagne": campagne.get("statut", ""),
        "campagne_id": None,
        "nb_communes": len(synth["communes"]),
        "nb_communes_avec_lien": t["lien_confirme"],
        "nb_communes_a_verifier": t["lien_incertain"] + t["aucun_lien_partiel"],
        "nb_communes_sans_lien": t["aucun_lien_complet"],
        "nb_communes_non_analysables": t["inaccessible"] + t["non_exploree"] + t["non_analysee"],
        "nb_communes_sans_site": t["sans_site"],
        "nb_liens_verifies": 0,
        "nb_liens_a_verifier": sum(len(c["liens_confirmes"]) for c in synth["communes"]),
    }
