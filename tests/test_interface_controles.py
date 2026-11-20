"""Contrôles de l'interface web (webapp.serve) : authentification, navigation, rapports, export, CSV, formulaires,
saisies hostiles, API d'état. Serveur HTTP local, source et runner factices conformes aux contrats (aucun réseau).
"""
import base64
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (chemins src/ et tests/)
import synthese  # noqa: E402
import webapp  # noqa: E402

MDP = "mdp-controle-42"
CAUSE = sorted(synthese.CAUSES)[0]
HOSTILE = '"><script>alert(1)</script>'
HOSTILE_ECHAPPE = "&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
NAVIGATION = {"/": "Accueil", "/analyse": "Analyse", "/suivi": "Suivi",
              "/rapports": "Rapports", "/resultats": "Résultats", "/campagnes": "Historique"}
# accueil protégé : menu d'administration de la vue publique (« À propos » et « Campagnes (administration) » en plus)
NAVIGATION_ACCUEIL = {"/": "Accueil", "/rapports": "Rapports", "/resultats": "Résultats", "/campagnes": "Historique",
                      "/a-propos": "À propos", "/analyse": "Analyse", "/suivi": "Suivi",
                      "/admin/campagnes": "Campagnes (administration)"}
PAGES = ["/", "/analyse", "/suivi", "/api/etat", "/rapports", "/rapports/60", "/rapports/60/export.html",
         "/rapports/60/communes.csv", "/campagnes", "/campagnes/7", "/resultats", "/resultats?dept=60&q=chat",
         "/resultats?page=9", "/csv/60", "/csv/tous"]

SYNTH_60 = {
    "departement": {"code": "60", "nom": "Oise"},
    "campagne": {"id": None, "debut": "2026-11-01T08:00:00", "fin": "2026-11-02T10:00:00", "statut": "terminee"},
    "totaux": {"lien_confirme": 1, "lien_incertain": 1, "aucun_lien_complet": 1, "aucun_lien_partiel": 0,
               "inaccessible": 1, "non_exploree": 0, "sans_site": 1, "non_analysee": 0},
    "communes": [
        {"code_insee": "60001", "nom": "=cmd()", "statut": "lien_confirme", "detail": "trouvé",
         "sites": [{"domaine": "a.fr", "url_depart": "https://a.fr/", "statut": "ok", "nb_pages": 10, "limites": [],
                    "erreurs": [], "cause": None, "tls_non_verifie": False, "source": "annuaire", "a_verifier": False}],
         "liens_confirmes": [{"url_page_source": "https://a.fr/contact",
                              "url_cible": "https://vosquestions.ecologie-territoires.gouv.fr/",
                              "texte": "Vos questions", "type_element": "lien", "verification": None, "remarque": ""}],
         "nb_liens_incertains": 0, "cause_principale": None, "site_a_verifier": False},
        {"code_insee": "60002", "nom": HOSTILE, "statut": "lien_incertain", "detail": "-2 points",
         "sites": [{"domaine": "b.fr", "url_depart": "https://b.fr/", "statut": "ok", "nb_pages": 3, "limites": [],
                    "erreurs": [], "cause": None, "tls_non_verifie": False, "source": "decouverte", "a_verifier": True}],
         "liens_confirmes": [], "nb_liens_incertains": 2, "cause_principale": None, "site_a_verifier": True},
        {"code_insee": "60003", "nom": "Aucun", "statut": "aucun_lien_complet", "detail": "", "sites": [],
         "liens_confirmes": [], "nb_liens_incertains": 0, "cause_principale": None, "site_a_verifier": False},
        {"code_insee": "60004", "nom": "Inacc", "statut": "inaccessible", "detail": "bloqué",
         "sites": [{"domaine": "c.fr", "url_depart": "https://c.fr/", "statut": "erreur", "nb_pages": 0,
                    "limites": [], "erreurs": ["timeout"], "cause": CAUSE, "tls_non_verifie": True,
                    "source": "annuaire", "a_verifier": False}],
         "liens_confirmes": [], "nb_liens_incertains": 0, "cause_principale": CAUSE, "site_a_verifier": False},
        {"code_insee": "60005", "nom": "Sans", "statut": "sans_site", "detail": "", "sites": [],
         "liens_confirmes": [], "nb_liens_incertains": 0, "cause_principale": None, "site_a_verifier": False},
    ],
}
ANALYSES = [
    {"id": 7, "statut": "echec", "departements": ["60", "80"], "debut": "2026-11-19T10:00:00",
     "fin": "2026-11-19T11:00:00", "journal": "[13] 25/130 sites ; total 25/130\n<script>x()</script>"},
    {"id": 6, "statut": "terminee", "departements": ["all"], "debut": "2026-11-10T10:00:00",
     "fin": "2026-11-10T20:00:00", "journal": "ok"},
]
RESUMES = [{"code": "60", "nom": "Oise", "date": "2026-11-02T10:00:00", "statut_campagne": "terminee",
            "campagne_id": 1, "nb_communes": 5, "nb_communes_avec_lien": 1, "nb_communes_a_verifier": 1,
            "nb_communes_sans_lien": 1, "nb_communes_non_analysables": 1, "nb_communes_sans_site": 1,
            "nb_liens_verifies": 0, "nb_liens_a_verifier": 0}]
LIENS = [{"dept": "60", "code_insee": "60001", "nom_commune": "=cmd() Châtillon", "url_page": "https://a.fr/contact"},
         {"dept": "80", "code_insee": "80001", "nom_commune": "Amiens", "url_page": "javascript:alert(1)"}]


class SourceTest:
    def depts(self):
        return ["60", "80"]

    def liens(self, dept=None):
        return [r for r in LIENS if dept is None or r["dept"] == dept]

    def tous_les_liens(self, q, dept, limit, offset):  # accueil : liste vide, les chiffres et menus restent testés
        return [], 0

    def nouveaux_liens(self, depuis, limit):
        return [], 0

    def get(self, dept, *, report=True, csv=True):
        return None

    def csv_all(self):
        return None

    def has_csv_all(self):
        return False

    def synthese(self, dept):
        return SYNTH_60 if dept == "60" else None

    def resumes(self):
        return RESUMES

    def analyses(self, limit=50):
        return ANALYSES[:limit]

    def analyse(self, aid):
        return next((a for a in ANALYSES if a["id"] == aid), None)


class RunnerTest:
    TAIL = "[13] 25/130 sites ; total 25/130"

    def __init__(self, actif=True, aid=7):
        self.actif, self._aid, self.demarrages = actif, aid, []
        self.started = time.time() - 30
        self.label = "60 80"

    @property
    def analysis_id(self):
        return self._aid

    def running(self):
        return self.actif

    def tail(self, lines=25):
        return self.TAIL

    def start(self, depts, resume):
        self.demarrages.append((depts, resume))
        return ""

    def stop(self):
        self.actif = False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class ServeurInterface(unittest.TestCase):
    """Un serveur neuf par test (le runner factice garde son état)."""
    actif, aid = False, None

    def setUp(self):
        self.runner = RunnerTest(actif=self.actif, aid=self.aid)
        root = Path(tempfile.mkdtemp())
        self.srv = webapp.serve(root, "127.0.0.1", 0, MDP, self.runner, SourceTest())
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.op = urllib.request.build_opener(NoRedirect)

    def appel(self, chemin, *, data=None, origin=None, auth=True, methode=None):
        """(statut, en-têtes, corps en octets). Les réponses 3xx/4xx/5xx ne lèvent pas d'exception."""
        corps = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        req = urllib.request.Request(self.base + chemin, data=corps, method=methode)
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"u:{MDP}".encode()).decode())
        if origin is not None:
            req.add_header("Origin", origin)
        try:
            with self.op.open(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def texte(self, chemin, **kw) -> str:
        return self.appel(chemin, **kw)[2].decode("utf-8")


class AuthentificationTests(ServeurInterface):
    def test_pages_protegees_401_sans_authentification(self):
        for p in PAGES:
            with self.subTest(page=p):
                self.assertEqual(self.appel(p, auth=False)[0], 401)

    def test_pages_200_avec_authentification(self):
        for p in PAGES:
            with self.subTest(page=p):
                statut = self.appel(p)[0]
                if p.startswith("/csv"):
                    self.assertIn(statut, (200, 404, 503))
                else:
                    self.assertEqual(statut, 200)

    def test_mauvais_mot_de_passe_refuse(self):
        req = urllib.request.Request(self.base + "/")
        req.add_header("Authorization", "Basic " + base64.b64encode(b"u:faux").decode())
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.op.open(req, timeout=5)
        self.assertEqual(e.exception.code, 401)

    def test_post_sans_authentification_401(self):
        self.assertEqual(self.appel("/analyse", data={"departement": "60"}, origin=self.base, auth=False)[0], 401)

    def test_ressources_statiques_sans_authentification(self):
        self.assertEqual(self.appel("/static/dsfr/dsfr/dsfr.min.css", auth=False)[0], 200)
        st, _, corps = self.appel("/static/app.js", auth=False)
        self.assertEqual(st, 200)
        self.assertIn(b"use strict", corps)
        self.assertEqual(self.appel("/static/dsfr/utility/icons/icons.min.css", auth=False)[0], 200)


class StructureTests(ServeurInterface):
    def test_un_seul_h1_par_page(self):
        for p in ["/", "/analyse", "/suivi", "/rapports", "/rapports/60", "/resultats", "/campagnes",
                  "/campagnes/7"]:
            with self.subTest(page=p):
                self.assertEqual(len(re.findall(r"<h1[ >]", self.texte(p))), 1)

    def test_menu_accueil_protegee_sans_tableau_de_bord(self):
        t = self.texte("/")
        liens = re.findall(r'<a class="fr-nav__link" href="([^"]+)" target="_self"', t)
        self.assertEqual(liens, list(NAVIGATION_ACCUEIL))
        self.assertNotIn("Tableau de bord", t)
        self.assertEqual(re.findall(r'href="([^"]+)" target="_self" aria-current="page">', t), ["/"])

    def test_menu_six_entrees_et_entree_active(self):
        for p, actif in [("/analyse", "/analyse"), ("/suivi", "/suivi"), ("/rapports", "/rapports"), ("/rapports/60", "/rapports"),
                         ("/resultats", "/resultats"), ("/campagnes", "/campagnes"), ("/campagnes/7", "/campagnes")]:
            with self.subTest(page=p):
                t = self.texte(p)
                liens = re.findall(r'<a class="fr-nav__link" href="([^"]+)" target="_self"', t)
                self.assertEqual(liens, list(NAVIGATION))
                self.assertEqual(re.findall(r'<a class="fr-nav__link" href="([^"]+)" target="_self" aria-current="page">', t),
                                 [actif])

    def test_page_d_erreur_garde_le_menu(self):
        t = self.texte("/campagnes/999")
        self.assertIn("Page introuvable", t)
        self.assertEqual(len(re.findall(r'<a class="fr-nav__link" href="[^"]+" target="_self"', t)), len(NAVIGATION))

    def test_pied_de_page_et_titre(self):
        t = self.texte("/")
        self.assertIn("Veille des liens Vos questions", t)
        self.assertIn("Résultats en lecture seule : aucune analyse ne peut être lancée ni modifiée depuis cette page.", t)
        self.assertIn('href="/a-propos"', t)
        self.assertNotIn("poste de l'agent", t)
        self.assertNotIn("Mettre à jour les sources", t)


class RapportsTests(ServeurInterface):
    def test_liste_des_rapports(self):
        t = self.texte("/rapports")
        self.assertIn('href="/rapports/60"', t)
        self.assertIn("Oise", t)
        self.assertIn('href="/rapports/60/communes.csv"', t)

    def test_sections_du_rapport_dans_l_ordre(self):
        t = self.texte("/rapports/60")
        sections = ["1. Communes avec un lien", "2. À vérifier", "3. Sites non analysables", "4. Aucun lien trouvé",
                    "5. Communes sans site identifié", "Tableau complet des communes"]
        pos = [t.find(s) for s in sections]
        self.assertTrue(all(p >= 0 for p in pos), pos)
        self.assertEqual(pos, sorted(pos))
        self.assertIn('data-statut="lien_confirme"', t)

    def test_rapport_sans_formulaire_ni_script(self):
        t = self.texte("/rapports/60")
        self.assertNotIn("<form", t)
        self.assertNotIn("/validations", t)
        self.assertNotIn("/corrections", t)
        self.assertNotIn("<script>", t)

    def test_saisie_hostile_du_nom_de_commune_echappee(self):
        t = self.texte("/rapports/60")
        self.assertNotIn(HOSTILE, t)
        self.assertIn(HOSTILE_ECHAPPE, t)

    def test_redirection_de_l_ancienne_adresse(self):
        st, en_tetes, _ = self.appel("/rapport/60")
        self.assertEqual((st, en_tetes.get("Location")), (303, "/rapports/60"))
        self.assertEqual(self.appel("/rapport/99")[0], 404)


class ExportTests(ServeurInterface):
    def test_export_autonome_sans_script_ni_lien_externe(self):
        st, en_tetes, corps = self.appel("/rapports/60/export.html")
        t = corps.decode("utf-8")
        self.assertEqual(st, 200)
        self.assertNotIn("<script", t.lower())
        self.assertIsNone(re.search(r'<link[^>]+href="https?://|src="https?://', t))
        self.assertNotIn("url(http", t)
        self.assertNotIn("<form", t)
        self.assertIn("attachment", en_tetes.get("Content-Disposition", ""))

    def test_export_avec_sections_repliables(self):
        t = self.texte("/rapports/60/export.html")
        self.assertIn("<details", t)
        self.assertIn("<summary", t)

    def test_css_autonome_sans_police_ni_url_externe(self):
        css = webapp._css_autonome()
        self.assertNotIn("@font-face", css)
        self.assertIsNone(re.search(r"url\(\s*['\"]?https?:", css))


class CsvCommunesTests(ServeurInterface):
    def test_entete_bom_et_separateur(self):
        _, _, corps = self.appel("/rapports/60/communes.csv")
        texte = corps.decode("utf-8-sig")
        self.assertEqual(texte.splitlines()[0], "Code INSEE;Commune;Statut;Liens trouvés;Détail")
        self.assertTrue(corps.startswith(b"\xef\xbb\xbf"))

    def test_formule_neutralisee(self):
        texte = self.appel("/rapports/60/communes.csv")[2].decode("utf-8-sig")
        self.assertIn("'=cmd()", texte)
        self.assertNotIn("\n=cmd()", texte)
        self.assertNotIn(";=cmd()", texte)

    def test_csv_global_et_departement_absents_404(self):
        self.assertEqual(self.appel("/csv/tous")[0], 404)
        self.assertEqual(self.appel("/csv/60")[0], 404)


class PagesDErreurTests(ServeurInterface):
    def test_404_en_page_dsfr(self):
        st, _, corps = self.appel("/campagnes/999")
        self.assertEqual(st, 404)
        self.assertIn(b"fr-", corps)
        self.assertIn("Page introuvable".encode(), corps)

    def test_departement_non_publie_et_chemins_inconnus(self):
        for p in ("/rapports/80", "/inconnu"):
            with self.subTest(page=p):
                self.assertEqual(self.appel(p)[0], 404)


class FormulairesTests(ServeurInterface):
    def test_origine_etrangere_ou_absente_refusee(self):
        self.assertEqual(self.appel("/analyse", data={"departement": ["60"]}, origin="https://pirate.example")[0], 403)
        self.assertEqual(self.appel("/analyse", data={"departement": ["60"]})[0], 403)
        self.assertEqual(self.appel("/arreter", data={}, origin="https://pirate.example")[0], 403)
        self.assertEqual(self.runner.demarrages, [])

    def test_formulaire_vide_message_d_erreur(self):
        st, _, corps = self.appel("/analyse", data={}, origin=self.base)
        self.assertEqual(st, 400)
        self.assertIn(b"Choisissez au moins un d", corps)
        self.assertIn(b"fr-alert--error", corps)

    def test_saisie_invalide_et_tous_sans_confirmation(self):
        self.assertEqual(self.appel("/analyse", data={"departement": ["60; rm"]}, origin=self.base)[0], 400)
        st, _, corps = self.appel("/analyse", data={"tous": "1"}, origin=self.base)
        self.assertEqual(st, 400)
        self.assertIn(b"cochez la case", corps)
        self.assertEqual(self.runner.demarrages, [])

    def test_lancement_valide_normalise_les_departements(self):
        st, en_tetes, _ = self.appel("/analyse", data={"departement": ["60", "1"], "reprise": "1"}, origin=self.base)
        self.assertEqual((st, en_tetes.get("Location")), (303, "/suivi"))
        self.assertEqual(self.runner.demarrages[-1], (["60", "01"], True))

    def test_tous_confirme_lance_toute_la_france(self):
        self.appel("/analyse", data={"tous": "1", "confirmer_tous": "1"}, origin=self.base)
        self.assertEqual(self.runner.demarrages[-1], (["all"], False))

    def test_arreter_et_reprises(self):
        self.assertEqual(self.appel("/arreter", data={}, origin=self.base)[1].get("Location"), "/suivi")
        self.assertEqual(self.appel("/reprendre/6", data={}, origin=self.base)[0], 404)          # terminée : pas reprenable
        st, en_tetes, _ = self.appel("/reprendre/7", data={}, origin=self.base)                # en échec : reprise
        self.assertEqual((st, en_tetes.get("Location")), (303, "/suivi"))
        self.assertEqual(self.runner.demarrages[-1], (["60", "80"], True))
        self.assertEqual(self.appel("/reprendre/999", data={}, origin=self.base)[0], 404)


class PageAnalyseTests(ServeurInterface):
    def test_cent_un_departements_et_nombre_de_communes(self):
        t = self.texte("/analyse")
        self.assertEqual(t.count('name="departement"'), 101)
        self.assertIn("60 Oise", t)
        self.assertIn("5 communes à la dernière analyse", t)

    def test_options_tous_confirmation_et_reprise(self):
        t = self.texte("/analyse")
        self.assertIn('name="tous"', t)
        self.assertIn('name="confirmer_tous"', t)
        self.assertIn('name="reprise"', t)
        self.assertNotIn("navigateur", t)
        self.assertNotIn("Rendu JavaScript", t)

    def test_reprises_listees(self):
        t = self.texte("/analyse")
        self.assertIn("/reprendre/7", t)
        self.assertNotIn("/reprendre/6", t)


class AccueilEtSuiviTests(ServeurInterface):
    actif, aid = False, 7                  # aucune analyse active ; la dernière (7) est en échec

    def test_accueil_chiffres_et_derniere_analyse(self):
        t = self.texte("/")
        corps = t[t.index("<main"):t.index("</main>")]
        self.assertIn("départements publiés", t)
        self.assertIn("communes à vérifier", t)
        self.assertIn("n° 7", corps)                       # administrateur : dernière analyse, quel que soit son statut
        self.assertIn("Échec", corps)
        self.assertNotIn('href="/campagnes/7"', corps)     # une analyse en échec n'a pas de lien public

    def test_api_etat_inactif_avec_analyse_en_echec(self):
        st, en_tetes, corps = self.appel("/api/etat")
        e = json.loads(corps)
        self.assertEqual(st, 200)
        self.assertIn("json", en_tetes.get("Content-Type", ""))
        self.assertFalse(e["actif"])
        self.assertEqual(e["statut"], "echec")
        self.assertTrue(e["erreur"])
        self.assertIsNone(e["progression"]["ecoule_s"])

    def test_suivi_inactif_sans_message(self):
        self.assertNotIn("Analyse en cours", self.texte("/suivi"))


class AnalyseActiveTests(ServeurInterface):
    actif, aid = True, 7

    def test_api_etat_actif_progression_et_cles(self):
        st, _, corps = self.appel("/api/etat")
        e = json.loads(corps)
        self.assertEqual(st, 200)
        self.assertTrue(e["actif"])
        self.assertEqual((e["statut"], e["campagne_id"], e["genre"]), ("en_cours", 7, "campagne"))
        self.assertEqual(set(e), {"actif", "genre", "statut", "campagne_id", "progression", "message", "erreur", "fichiers"})
        self.assertEqual(set(e["progression"]), {"sites_traites", "sites_total", "pages_visitees", "liens_confirmes",
                                                 "ecoule_s", "restant_estime_s"})
        self.assertEqual((e["progression"]["sites_traites"], e["progression"]["sites_total"]), (25, 130))
        self.assertGreaterEqual(e["progression"]["ecoule_s"], 29)
        self.assertGreater(e["progression"]["restant_estime_s"], 0)

    def test_suivi_actif_bouton_arreter_active_et_actualisation_manuelle(self):
        t = self.texte("/suivi")
        bouton = re.search(r'<button[^>]*id="btn-arreter"[^>]*>', t).group(0)
        self.assertNotIn("disabled", bouton)
        self.assertIn('href="/suivi"', t)
        self.assertIn("Actualiser", t)
        self.assertNotIn("actualiser-auto", t)
        self.assertEqual(len(re.findall(r"<h1[ >]", t)), 1)


class CampagneEtResultatsTests(ServeurInterface):
    def test_journal_de_campagne_echappe(self):
        t = self.texte("/campagnes/7")
        self.assertIn('<pre class="mlm-journal">', t)
        self.assertIn("&lt;script&gt;x()", t)
        self.assertNotIn("<script>x()", t)
        self.assertIn('href="/rapports/60"', t)
        self.assertNotIn("Bilan", t)

    def test_liste_des_campagnes(self):
        self.assertIn("7</a>", self.texte("/campagnes"))

    def test_recherche_hostile_echappee(self):
        t = self.texte("/resultats?q=" + urllib.parse.quote(HOSTILE))
        self.assertNotIn("<script>alert(1)", t)
        self.assertIn("&quot;&gt;&lt;script&gt;alert(1)", t)

    def test_departement_hostile_ignore(self):
        st, _, corps = self.appel("/resultats?dept=%3Cscript%3E")
        self.assertEqual(st, 200)
        self.assertNotIn(b"<script>", corps)

    def test_lien_javascript_non_cliquable(self):
        self.assertNotIn(b'href="javascript', self.appel("/resultats")[2])
