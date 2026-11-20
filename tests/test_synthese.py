"""Synthèse par département (synthese.synthese_departement, resume) et référentiel des départements.

Chaque statut de commune de la règle de synthese.py est produit par un cas d'un petit département écrit à la main.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (chemins src/ et tests/)
import csvio  # noqa: E402
import history  # noqa: E402
import synthese as S  # noqa: E402
from departements import DEPARTEMENTS, nom_departement  # noqa: E402
from layout import DeptPaths  # noqa: E402

CIBLE = "https://vosquestions.ecologie-territoires.gouv.fr/60001-x"
COLONNES_SITE = ["code_insee", "nom_commune", "site_url", "confiance_lot2", "url_utilisee", "confiance_finale",
                 "statut_crawl", "motif", "ssl_invalide", "pages_visitees", "limite_pages_atteinte",
                 "limite_profondeur_atteinte", "pages_tronquees", "erreur"]
COLONNES_LIEN = ["code_insee", "nom_commune", "site_url", "page_url", "target_url", "href_origine", "texte_lien",
                 "type_lien", "zone", "cible_commune", "confiance_site", "ssl_invalide", "date_controle"]
CLES_RESUME = {"code", "nom", "date", "statut_campagne", "campagne_id", "nb_communes", "nb_communes_avec_lien",
               "nb_communes_a_verifier", "nb_communes_sans_lien", "nb_communes_non_analysables",
               "nb_communes_sans_site", "nb_liens_verifies", "nb_liens_a_verifier"}


def site(code, nom, url, statut, motif="", erreur="", pages="1", **extra):
    ligne = {c: "" for c in COLONNES_SITE}
    ligne.update(code_insee=code, nom_commune=nom, site_url=url, confiance_lot2="probable_officiel", statut_crawl=statut,
                 motif=motif, erreur=erreur, pages_visitees=pages, ssl_invalide="0", limite_pages_atteinte="0",
                 limite_profondeur_atteinte="0", pages_tronquees="0")
    ligne.update(extra)
    return ligne


def lien(code, nom, site_url, page, texte):
    ligne = {c: "" for c in COLONNES_LIEN}
    ligne.update(code_insee=code, nom_commune=nom, site_url=site_url, page_url=page, target_url=CIBLE, texte_lien=texte,
                 type_lien="lien", zone="contenu", cible_commune="oui", confiance_site="probable_officiel", ssl_invalide="0")
    return ligne


def communes(*paires):
    """paires (code, nom, nb_urls_valides) -> lignes de l'inventaire."""
    return [{"code_insee": c, "nom_commune": n, "nb_urls_valides": str(u), "statut_commune": "x"} for c, n, u in paires]


class SyntheseTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = history.db_path(self.root)

    def departement(self, inventaire, crawl=(), liens=(), code="60") -> dict | None:
        p = DeptPaths(self.root, code)
        p.reports_dir.mkdir(parents=True, exist_ok=True)
        csvio.write_rows(p.inventory_communes, inventaire)
        if crawl:
            csvio.write_rows(p.crawl_sites, list(crawl))
        if liens:
            csvio.write_rows(p.links_found, list(liens))
        history.record(p, self.db)
        return S.synthese_departement(p, self.db)

    def statuts(self, synth: dict) -> dict[str, str]:
        return {c["code_insee"]: c["statut"] for c in synth["communes"]}


class StatutsTests(SyntheseTestCase):
    def test_un_cas_par_statut(self):
        synth = self.departement(
            communes(("60001", "Alpha", 1), ("60002", "Bêta", 1), ("60003", "Gamma", 1), ("60004", "Delta", 1),
                     ("60005", "Epsilon", 1), ("60006", "Zéta", 1), ("60007", "Eta", 0)),
            crawl=[
                site("60001", "Alpha", "https://alpha.fr/", "lien_trouve", "arrêt au premier lien trouvé", pages="2"),
                site("60002", "Bêta", "https://beta.fr/", "explore_complet",
                     "URL du portail en texte brut sans lien sur https://beta.fr/actu", pages="5"),
                site("60003", "Gamma", "https://gamma.fr/", "explore_complet", "toutes les pages atteignables", pages="9"),
                site("60004", "Delta", "https://delta.fr/", "explore_partiel", "durée maximale par site atteinte", pages="3",
                     limite_pages_atteinte="1"),
                site("60005", "Epsilon", "https://epsilon.fr/", "erreur_acces", "page d'accueil : erreur_dns",
                     erreur="https://epsilon.fr/ -> erreur_dns Name or service not known", pages="0"),
                site("60006", "Zéta", "https://zeta.fr/", "non_explore", "non exploré : confiance=tiers_probable", pages="0"),
            ],
            liens=[lien("60001", "Alpha", "https://alpha.fr/", "https://alpha.fr/", "Cliquez ici")])
        self.assertEqual(self.statuts(synth), {
            "60001": "lien_confirme",      # lien cliquable vers la cible
            "60002": "lien_incertain",     # URL du portail en texte brut
            "60003": "aucun_lien_complet", # site lu en entier sans lien
            "60004": "aucun_lien_partiel", # site lu partiellement sans lien
            "60005": "inaccessible",       # aucun site lu, erreur d'accès
            "60006": "non_analysee",       # site écarté avant exploration
            "60007": "sans_site",          # aucun site identifié
        })

    def test_sans_site_sans_exploration(self):
        synth = self.departement(communes(("60007", "Eta", 0)))
        self.assertEqual(self.statuts(synth), {"60007": "sans_site"})

    def test_site_connu_sans_exploration_donne_non_analysee(self):
        synth = self.departement(communes(("60008", "Théta", 2)))
        self.assertEqual(self.statuts(synth), {"60008": "non_analysee"})

    def test_robots_interdit_compte_comme_inaccessible(self):
        synth = self.departement(
            communes(("60009", "Iota", 1)),
            crawl=[site("60009", "Iota", "https://iota.fr/", "robots_interdit", "robots.txt interdit la page d'accueil",
                        pages="0")])
        self.assertEqual(self.statuts(synth), {"60009": "inaccessible"})
        self.assertEqual(synth["communes"][0]["cause_principale"], "interdit_robots")

    def test_lien_confirme_l_emporte_sur_texte_brut(self):
        synth = self.departement(
            communes(("60010", "Kappa", 1)),
            crawl=[site("60010", "Kappa", "https://kappa.fr/", "explore_complet",
                        "URL du portail en texte brut sur https://kappa.fr/a", pages="4")],
            liens=[lien("60010", "Kappa", "https://kappa.fr/", "https://kappa.fr/a", "Vos questions")])
        self.assertEqual(self.statuts(synth), {"60010": "lien_confirme"})

    def test_totaux_couvrent_toutes_les_communes(self):
        synth = self.departement(
            communes(("60001", "Alpha", 1), ("60002", "Bêta", 1), ("60007", "Eta", 0)),
            crawl=[site("60001", "Alpha", "https://alpha.fr/", "lien_trouve", "arrêt", pages="2"),
                   site("60002", "Bêta", "https://beta.fr/", "erreur_acces", "page d'accueil : timeout")],
            liens=[lien("60001", "Alpha", "https://alpha.fr/", "https://alpha.fr/", "Cliquez ici")])
        t = synth["totaux"]
        self.assertEqual((t["lien_confirme"], t["inaccessible"], t["sans_site"]), (1, 1, 1))
        self.assertEqual(sum(t.values()), len(synth["communes"]))      # chaque commune est comptée une fois
        self.assertEqual(len(synth["communes"]), 3)

    def test_departement_sans_inventaire_donne_none(self):
        p = DeptPaths(self.root, "02")
        p.reports_dir.mkdir(parents=True)
        self.assertIsNone(S.synthese_departement(p, self.db))


class ResumeTests(SyntheseTestCase):
    def test_cles_exactes(self):
        synth = self.departement(communes(("60001", "Alpha", 1)))
        self.assertEqual(set(S.resume(synth)), CLES_RESUME)

    def test_compteurs_par_statut(self):
        synth = self.departement(
            communes(("60001", "Alpha", 1), ("60002", "Bêta", 1), ("60003", "Gamma", 1), ("60007", "Eta", 0)),
            crawl=[site("60001", "Alpha", "https://alpha.fr/", "lien_trouve", "arrêt", pages="2"),
                   site("60002", "Bêta", "https://beta.fr/", "explore_complet", "toutes les pages atteignables", pages="9"),
                   site("60003", "Gamma", "https://gamma.fr/", "erreur_acces", "page d'accueil : timeout")],
            liens=[lien("60001", "Alpha", "https://alpha.fr/", "https://alpha.fr/", "Cliquez ici")])
        r = S.resume(synth)
        self.assertEqual((r["code"], r["nom"]), ("60", "Oise"))
        self.assertEqual(r["nb_communes"], 4)
        self.assertEqual(r["nb_communes_avec_lien"], 1)
        self.assertEqual(r["nb_communes_sans_lien"], 1)
        self.assertEqual(r["nb_communes_non_analysables"], 1)
        self.assertEqual(r["nb_communes_sans_site"], 1)
        self.assertEqual(r["nb_liens_a_verifier"], 1)
        self.assertEqual(r["nb_liens_verifies"], 0)

    def test_date_et_campagne(self):
        synth = self.departement(communes(("60001", "Alpha", 1)))
        r = S.resume(synth)
        self.assertTrue(r["date"])
        self.assertEqual(r["statut_campagne"], synth["campagne"]["statut"])


class DepartementsTests(unittest.TestCase):
    def test_101_entrees(self):
        self.assertEqual(len(DEPARTEMENTS), 101)

    def test_noms_corse_et_oise(self):
        self.assertEqual(nom_departement("2A"), "Corse-du-Sud")
        self.assertEqual(nom_departement("60"), "Oise")


if __name__ == "__main__":
    unittest.main()
