"""Lecture publique (MLM_LECTURE_PUBLIQUE) servie par webapp : liste blanche des routes publiques, refus par défaut des
autres, menu adaptatif selon l'identifiant, en-têtes, échappement, CSV, pannes. Serveur réel sur 127.0.0.1, source
fichiers (FileSource). Aucun réseau. La variante Postgres du même contrat est dans test_consultation_postgres.py ;
l'activation par variable d'environnement dans test_consultation_api.py.
"""
import base64
import csv
import io
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
import csvio  # noqa: E402
import webapp  # noqa: E402
from layout import DeptPaths  # noqa: E402
from publications import FileSource, SourceIndisponible  # noqa: E402

MOT_DE_PASSE = "mot-de-passe-lecture-test-4b2f"
PUBLIQUES = ("/", "/a-propos", "/departement/60", "/departement/60.csv", "/rapports", "/rapports/60",
             "/rapports/60/communes.csv", "/rapports/60/export.html", "/campagnes")
# Protégées même avec la lecture publique : lancement, suivi, administration (/admin/...), formes non listées.
PROTEGEES = ("/analyse", "/suivi", "/api/etat", "/admin/campagnes", "/admin/campagnes/1", "/admin/rapports/60",
             "/resultats", "/csv/60", "/csv/tous", "/lancer", "/reprendre/1", "/arreter", "/tableau-de-bord",
             "/inconnu", "/a-propos/", "/departement/60/x", "/departement/60.json", "/departement/abcd",
             "/rapports/60/", "/rapports/60/x", "/rapports/60.csv", "/rapports/60/export.json", "/rapports/abcd",
             "/rapport/60/x", "/campagnes/1/x", "/campagnes/1234567890", "/campagnes/1.json", "/campagnes/-1")
MENU_ADMIN = ('href="/suivi"', 'href="/rapports"', 'href="/campagnes"')
MENU_EXPLOITATION = ('href="/suivi"', 'href="/analyse"', 'href="/tableau-de-bord"', 'href="/admin/campagnes"',
                     'href="/api/etat"')
MENTION = ("Résultats générés automatiquement à partir de pages publiques ; une absence de lien peut venir d'un site "
           "inaccessible ou d'un lien non détecté.")
FORMULES = ("=", "+", "-", "@", "\t", "\r")
IMG_HOSTILE = "<img src=x onerror=alert(1)>"
FORMULE_HOSTILE = '=HYPERLINK("https://evil.example","clic")'


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_SansRedirection)


def construire_racine_hostile(root: Path) -> None:
    """Racine de test avec des noms de communes hostiles (formule tableur, balise HTML) dans le département 60."""
    sp.construire_racine(root)
    csvio.write_rows(DeptPaths(root, "60").inventory_communes, [
        {"code_insee": "60001", "nom_commune": FORMULE_HOSTILE, "nb_urls_valides": "1"},
        {"code_insee": "60002", "nom_commune": IMG_HOSTILE, "nb_urls_valides": "1"},
        {"code_insee": "60003", "nom_commune": "@SUM(1+1)", "nb_urls_valides": "1"},
    ])


class PanneSource:
    """Source dont toutes les lectures échouent comme une base injoignable (SourceIndisponible)."""

    def __init__(self, racine: Path):
        self._source = FileSource(racine)

    def __getattr__(self, nom):
        def en_panne(*args, **kwargs):
            raise SourceIndisponible(nom)
        return en_panne


def basic(identifiant: str) -> str:
    return "Basic " + base64.b64encode(identifiant.encode()).decode()


class ServeurLecture(unittest.TestCase):
    """Serveur réel ; `lecture` active ou non la lecture publique. Les sous-classes adaptent la racine et la source."""

    lecture = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.preparer(self.root)
        self.runner = webapp.Runner(self.root)
        self.serveur = webapp.serve(self.root, "127.0.0.1", 0, MOT_DE_PASSE, self.runner, self.creer_source(),
                                    lecture_publique=self.lecture)
        self.addCleanup(self._arreter)
        threading.Thread(target=self.serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.serveur.server_address[1]}"

    def _arreter(self):
        self.serveur.shutdown()
        self.serveur.server_close()

    def preparer(self, root: Path) -> None:
        sp.construire_racine(root)

    def creer_source(self):
        return FileSource(self.root)

    def appel(self, methode: str, chemin: str, auth: str | None = None, origine: bool = False,
              corps: bytes | None = None, brut_auth: str | None = None):
        """Retourne (statut, en-têtes, corps texte). `auth` = mot de passe (identifiant « x ») ; `brut_auth` = en-tête brut."""
        en_tetes = {}
        if auth is not None:
            en_tetes["Authorization"] = basic(f"x:{auth}")
        if brut_auth is not None:
            en_tetes["Authorization"] = brut_auth
        if origine:
            en_tetes["Origin"] = self.base  # origine valide : un formulaire accepté ailleurs le serait ici
        req = urllib.request.Request(self.base + chemin, data=corps, method=methode, headers=en_tetes)
        try:
            with OPENER.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def html(self, chemin: str, auth: str | None = None) -> str:
        statut, _, corps = self.appel("GET", chemin, auth=auth)
        self.assertEqual(statut, 200, chemin)
        return corps


def attributs_href_action(html: str) -> list[str]:
    return re.findall(r'(?:href|action)="([^"]*)"', html)


class LectureDesactivee(ServeurLecture):
    """MLM_LECTURE_PUBLIQUE absente : comportement d'origine, tout derrière le mot de passe, racine comprise."""

    lecture = False

    def test_racine_protegee(self):
        self.assertEqual(self.appel("GET", "/")[0], 401)
        self.assertEqual(self.appel("GET", "/", auth=MOT_DE_PASSE)[0], 200)
        self.assertIn("Lancer une analyse", self.html("/", auth=MOT_DE_PASSE))  # racine = tableau de bord, comme avant
        self.assertEqual(self.appel("GET", "/tableau-de-bord")[0], 401)
        self.assertIn("Lancer une analyse", self.html("/tableau-de-bord", auth=MOT_DE_PASSE))

    def test_pages_publiques_ne_s_ouvrent_pas(self):
        for chemin in ("/a-propos", "/departement/60", "/departement/60.csv", "/rapports", "/rapports/60",
                       "/rapports/60/communes.csv", "/rapports/60/export.html", "/rapport/60", "/campagnes"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 401)
        self.assertEqual(self.appel("GET", "/departement/60", auth=MOT_DE_PASSE)[0], 404)  # hors liste : comme avant

    def test_rapports_et_campagnes_gardent_la_vue_administrateur(self):
        # Sans lecture publique, /rapports et /campagnes restent les vues d'administration complètes.
        self.assertIn("Détails techniques", self.html("/rapports/60", auth=MOT_DE_PASSE))
        self.assertIn('href="/rapports/60"', self.html("/rapports", auth=MOT_DE_PASSE))
        self.assertIn("Aucune analyse pour le moment.", self.html("/campagnes", auth=MOT_DE_PASSE))
        self.assertEqual(self.appel("GET", "/admin/rapports/60", auth=MOT_DE_PASSE)[0], 200)
        self.assertEqual(self.appel("GET", "/admin/campagnes", auth=MOT_DE_PASSE)[0], 200)
        self.assertEqual(self.appel("GET", "/admin/campagnes", auth="mauvais")[0], 401)

    def test_post_sur_racine_toujours_protege(self):
        self.assertEqual(self.appel("POST", "/", origine=True, corps=b"dept=60")[0], 401)
        self.assertIsNone(self.runner.proc)

    def test_head_et_verbes_inchanges(self):
        self.assertEqual(self.appel("HEAD", "/")[0], 501)
        self.assertEqual(self.appel("PUT", "/analyse", auth=MOT_DE_PASSE)[0], 501)


class ContratLecture:
    """Contrat de la lecture publique (mixin) : repris à l'identique par les variantes fichiers et Postgres.
    Utilise self.appel et self.html, fournis par ServeurLecture."""

    def test_pages_publiques_sans_authentification(self):
        for chemin in PUBLIQUES:
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 200)
        self.assertEqual(self.appel("HEAD", "/")[:1], (200,))
        self.assertEqual(self.appel("HEAD", "/")[2], "")

    def test_liste_blanche_et_refus_par_defaut(self):
        for chemin in PROTEGEES:
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 401)
                self.assertEqual(self.appel("GET", chemin, brut_auth="Basic n-importe-quoi")[0], 401)

    def test_protegees_accessibles_avec_identifiants_valides(self):
        self.assertEqual(self.appel("GET", "/analyse", auth=MOT_DE_PASSE)[0], 200)
        self.assertEqual(self.appel("GET", "/suivi", auth=MOT_DE_PASSE)[0], 200)

    def test_post_sur_publiques_405_meme_avec_identifiants_et_origine(self):
        for methode in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "FOOBAR"):
            for chemin in PUBLIQUES:
                with self.subTest(methode=methode, chemin=chemin):
                    statut, en_tetes, _ = self.appel(methode, chemin, auth=MOT_DE_PASSE, origine=True, corps=b"dept=60")
                    self.assertEqual(statut, 405)
                    self.assertEqual(en_tetes.get("Allow"), "GET, HEAD")
        self.assertIsNone(self.runner.proc)  # aucun POST ne lance d'analyse

    def test_post_sur_protegees_exige_le_mot_de_passe(self):
        for chemin in ("/analyse", "/lancer", "/arreter", "/reprendre/1", "/departement/60/x"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("POST", chemin, origine=True, corps=b"dept=60")[0], 401)
        self.assertIsNone(self.runner.proc)

    def test_racine_visiteur_sans_menu_admin_ni_lancement(self):
        html = self.html("/")
        self.assertIn(MENTION, html)
        self.assertIn("Résultats", html)
        self.assertRegex(html, r'href="/analyse"[^>]*>Espace réservé</a>')
        for lien in MENU_EXPLOITATION:
            if lien != 'href="/analyse"':  # « Espace réservé » : contrat existant de l'accueil visiteur
                self.assertNotIn(lien, html)
        self.assertIn('href="/rapports"', html)            # rapports et historique : publics, dans le menu visiteur
        self.assertIn('href="/campagnes"', html)
        self.assertNotIn("Lancer une analyse", html)
        self.assertNotIn("Analyse en cours", html)

    def test_liens_et_formulaires_visiteur(self):
        for chemin in ("/", "/a-propos", "/departement/60", "/rapports", "/rapports/60", "/campagnes"):
            with self.subTest(chemin=chemin):
                html = self.html(chemin)
                self.assertNotIn('<form method="post"', html.lower())
                for valeur in attributs_href_action(html):
                    self.assertFalse(valeur.startswith("javascript:"), valeur)
                    if valeur.startswith("/"):
                        chemin_seul = valeur.split("#")[0].split("?")[0]  # ancre et filtres : même route publique
                        self.assertTrue(chemin_seul in ("/", "/a-propos", "/analyse", "/resultats.csv", "/rapports",
                                                        "/campagnes")
                                        or chemin_seul.startswith(("/departement/", "/rapports/", "/campagnes/",
                                                                   "/static/")), valeur)
                if chemin in ("/", "/a-propos", "/departement/60"):
                    self.assertEqual(attributs_href_action(html).count("/analyse"), 1)  # Espace réservé, une seule fois
                else:
                    self.assertNotIn("/analyse", attributs_href_action(html))  # pages assainies : aucun lien d'exploitation
                    for valeur in MENU_EXPLOITATION:
                        self.assertNotIn(valeur, html)

    def test_identifiants_valides_menu_complet(self):
        html = self.html("/", auth=MOT_DE_PASSE)
        for lien in MENU_ADMIN + ('href="/analyse"', 'href="/tableau-de-bord"'):
            self.assertIn(lien, html)
        self.assertIn(MENTION, html)                       # la racine garde la vue résultats pour l'administrateur
        self.assertIn('id="tous-les-liens"', html)
        self.assertNotIn("Lancer une analyse", html)       # aucun bouton de lancement dans la vue résultats
        self.assertNotIn('method="post"', html.lower())
        self.assertIn('href="/suivi"', self.html("/a-propos", auth=MOT_DE_PASSE))
        self.assertIn('href="/suivi"', self.html("/departement/60", auth=MOT_DE_PASSE))

    def test_tableau_de_bord_protege_et_complet(self):
        self.assertEqual(self.appel("GET", "/tableau-de-bord")[0], 401)
        self.assertEqual(self.appel("GET", "/tableau-de-bord", brut_auth="Basic n-importe-quoi")[0], 401)
        html = self.html("/tableau-de-bord", auth=MOT_DE_PASSE)
        self.assertIn("Lancer une analyse", html)          # ancien tableau de bord d'administration
        self.assertIn('href="/tableau-de-bord"', html)     # entrée de menu active
        self.assertIn('aria-current="page">Tableau de bord</a>', html)

    def test_en_tete_authorization_invalide_reste_public(self):
        for brut in (basic("x:mauvais"), "Basic ###", "Bearer jeton", "Basic " + base64.b64encode(b"sansdeuxpoints").decode()):
            with self.subTest(brut=brut):
                statut, _, corps = self.appel("GET", "/", brut_auth=brut)
                self.assertEqual(statut, 200)
                self.assertNotIn('href="/suivi"', corps)
                self.assertEqual(self.appel("GET", "/a-propos", brut_auth=brut)[0], 200)
                self.assertEqual(self.appel("GET", "/departement/60.csv", brut_auth=brut)[0], 200)

    def test_en_tetes_noindex_et_cache_vary(self):
        for chemin in ("/", "/a-propos", "/departement/60", "/departement/99", "/rapports", "/rapports/60",
                       "/rapports/60/communes.csv", "/rapports/60/export.html", "/campagnes", "/campagnes/1"):
            with self.subTest(chemin=chemin):
                statut, en_tetes, corps = self.appel("GET", chemin)
                self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")
                self.assertEqual(en_tetes.get("Cache-Control"), "no-store")
                self.assertIn("Authorization", en_tetes.get("Vary", ""))
                self.assertIsNone(en_tetes.get("Set-Cookie"))
                self.assertEqual(en_tetes.get("X-Content-Type-Options"), "nosniff")
                self.assertIn("frame-ancestors 'none'", en_tetes.get("Content-Security-Policy", ""))
                if statut == 200 and chemin != "/rapports/60/export.html" and "text/html" in en_tetes.get("Content-Type", ""):
                    self.assertIn('<meta name="robots" content="noindex,nofollow">', corps)

    def test_mentions_obligatoires_sur_chaque_page_publique(self):
        for chemin in ("/", "/a-propos", "/departement/60", "/rapports", "/rapports/60", "/campagnes"):
            with self.subTest(chemin=chemin):
                html = self.html(chemin)
                self.assertIn(MENTION, html)
                self.assertIn("Date de la dernière publication", html)

    def test_une_seule_titre_1_par_page(self):
        for chemin in ("/", "/a-propos", "/departement/60", "/departement/99", "/rapports", "/rapports/60", "/campagnes",
                       "/campagnes/1"):
            with self.subTest(chemin=chemin):
                self.assertEqual(len(re.findall(r"<h1[\s>]", self.appel("GET", chemin)[2])), 1)

    def test_liste_et_recherche_des_departements(self):
        html = self.html("/")
        self.assertIn("Oise", html)
        self.assertIn("Somme", html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("<caption>", html)
        self.assertIn('<th scope="col">Dernière publication</th>', html)
        self.assertIn('href="/departement/60.csv"', html)
        filtre = self.html("/?q=SOMME")  # sans accents ni casse
        self.assertIn("Somme", filtre)
        self.assertNotIn('href="/departement/60"', filtre)  # le tableau départements est filtré (la liste déroulante des liens ne l'est pas)
        self.assertIn("Aucun département", self.html("/?q=zzz-rien"))
        self.assertIn("Oise", self.html("/?q=60"))

    def test_departement_sans_details_techniques(self):
        html = self.html("/departement/60")
        self.assertIn("<h1>Oise (60)</h1>", html)
        self.assertIn("Télécharger la liste des communes (CSV)", html)
        self.assertIn('href="/departement/60.csv"', html)
        self.assertNotIn("Détails techniques", html)
        self.assertNotIn("Liens écartés", html)
        self.assertNotIn("Valeurs brutes", html)
        self.assertNotIn("Télécharger (HTML)", html)
        self.assertIn("<details", html)  # accordéons sans script

    def test_liens_externes_en_nouvelle_fenetre_nofollow(self):
        html = self.html("/departement/60")
        balises = re.findall(r'<a [^>]*target="_blank"[^>]*>', html)
        self.assertTrue(balises)
        for balise in balises:
            self.assertIn('rel="noopener noreferrer nofollow"', balise)
        self.assertIn("(nouvelle fenêtre)", html)

    def test_departement_inconnu_404_et_code_invalide_protege(self):
        self.assertEqual(self.appel("GET", "/departement/99")[0], 404)
        self.assertEqual(self.appel("GET", "/departement/2A")[0], 404)
        for chemin in ("/departement/abcd", "/departement/6", "/departement/60.json", "/departement/60/"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 401)  # hors liste blanche : protégé par défaut
                self.assertEqual(self.appel("GET", chemin, auth=MOT_DE_PASSE)[0], 404)

    def test_csv_departement(self):
        statut, en_tetes, corps = self.appel("GET", "/departement/60.csv")
        self.assertEqual(statut, 200)
        self.assertTrue(en_tetes.get("Content-Type", "").startswith("text/csv"))
        self.assertIn('filename="communes_60.csv"', en_tetes.get("Content-Disposition", ""))
        self.assertTrue(corps.startswith("﻿Code INSEE;Commune;Statut"))

    def test_csv_sans_formule_active(self):
        octets = self.appel("GET", "/departement/60.csv")[2].lstrip("﻿")
        for ligne in csv.reader(io.StringIO(octets), delimiter=";"):
            for cellule in ligne:
                self.assertFalse(cellule.startswith(FORMULES), cellule)

    def test_recherche_hostile_echappee(self):
        hostile = self.html('/?q="><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)", hostile)
        self.assertNotIn('"><script>', hostile)

    def test_head_sans_corps_avec_les_memes_en_tetes(self):
        statut, en_tetes, corps = self.appel("HEAD", "/departement/60")
        self.assertEqual((statut, corps), (200, ""))
        self.assertGreater(int(en_tetes.get("Content-Length", "0")), 0)
        self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")

    def test_secrets_absents_des_reponses(self):
        for chemin in ("/", "/departement/60", "/analyse"):
            statut, en_tetes, corps = self.appel("GET", chemin, brut_auth=basic("x:faux"))
            texte = corps + "\n".join(f"{k}: {v}" for k, v in en_tetes.items())
            self.assertNotIn(MOT_DE_PASSE, texte)


class LectureFichiers(ContratLecture, ServeurLecture):
    """Variante fichiers (FileSource), racine de test standard : 60 complet, 80 sans CSV."""

    lecture = True

    def test_departement_sans_csv_reste_consultable(self):
        self.assertIn("Somme", self.html("/departement/80"))
        self.assertEqual(self.appel("GET", "/departement/80.csv")[0], 200)


class LectureHostile(ServeurLecture):
    """Noms de communes hostiles : échappés dans les pages, aucune formule active dans le CSV."""

    lecture = True

    def preparer(self, root: Path) -> None:
        construire_racine_hostile(root)

    def test_saisie_hostile_echappee(self):
        html = self.html("/departement/60")
        self.assertNotIn(IMG_HOSTILE, html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("javascript:", html.lower())
        self.assertNotIn("<img src", html)

    def test_csv_neutralise(self):
        octets = self.appel("GET", "/departement/60.csv")[2]
        cellules = [c for ligne in csv.reader(io.StringIO(octets.lstrip("﻿")), delimiter=";") for c in ligne]
        self.assertIn("'" + FORMULE_HOSTILE, cellules)  # apostrophe ajoutée : la formule n'est pas exécutée
        self.assertIn("'@SUM(1+1)", cellules)
        for cellule in cellules:
            self.assertFalse(cellule.startswith(FORMULES), cellule)

    def test_recherche_hostile_echappee(self):
        html = self.html("/?q=" + "%3Cimg%20src%3Dx%3E")
        self.assertNotIn("<img src=x>", html)
        self.assertIn("&lt;img src=x&gt;", html)


class LecturePanne(ServeurLecture):
    """Base injoignable : pages de données en 503 sobre ; À propos reste lisible ; la racine admin garde son alerte."""

    lecture = True

    def creer_source(self):
        return PanneSource(self.root)

    def test_pages_de_donnees_503_sans_detail_technique(self):
        for chemin in ("/", "/departement/60", "/departement/60.csv", "/rapports", "/rapports/60",
                       "/rapports/60/communes.csv", "/rapports/60/export.html", "/campagnes"):
            with self.subTest(chemin=chemin):
                statut, en_tetes, corps = self.appel("GET", chemin)
                self.assertEqual(statut, 503)
                self.assertIn("Service indisponible", corps)
                self.assertIn("ne sont pas disponibles", corps)
                self.assertNotIn("Traceback", corps)
                self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")

    def test_a_propos_lisible_et_date_indisponible(self):
        statut, _, corps = self.appel("GET", "/a-propos")
        self.assertEqual(statut, 200)
        self.assertIn("Date de la dernière publication indisponible", corps)
        self.assertIn("Limites", corps)

    def test_racine_admin_503_comme_les_visiteurs_tableau_de_bord_garde_son_alerte(self):
        statut, _, corps = self.appel("GET", "/", auth=MOT_DE_PASSE)
        self.assertEqual(statut, 503)                      # résultats publics : même réponse que pour un visiteur
        self.assertIn("ne sont pas disponibles", corps)
        statut, _, corps = self.appel("GET", "/tableau-de-bord", auth=MOT_DE_PASSE)
        self.assertEqual(statut, 200)
        self.assertIn("Base de données indisponible", corps)


def principal(html: str) -> str:
    """Contenu de <main> : la partie qui doit être identique pour un visiteur et un administrateur."""
    return html[html.index("<main"):html.index("</main>")]


class LectureRapports(ServeurLecture):
    """Rapports publics : vue assainie identique pour tous, CSV et export autonome, ancienne adresse redirigée."""

    lecture = True

    def test_liste_des_rapports(self):
        html = self.html("/rapports")
        self.assertIn("<h1>Rapports par département</h1>", html)
        self.assertIn("<caption>", html)
        self.assertIn('<th scope="col">Département</th>', html)
        self.assertIn('href="/rapports/60"', html)
        self.assertIn('href="/rapports/60/communes.csv"', html)
        self.assertIn('href="/rapports/60/export.html"', html)
        self.assertNotIn("Détails techniques", html)

    def test_rapport_sans_details_techniques_ni_liens_d_exploitation(self):
        html = self.html("/rapports/60")
        self.assertIn("<h1>Oise (60)</h1>", html)
        self.assertIn("Télécharger la liste des communes (CSV)", html)
        self.assertIn("Télécharger le compte rendu (HTML)", html)
        self.assertIn("Retour à la liste des rapports", html)
        for absent in ("Détails techniques", "Liens écartés", "Valeurs brutes", "Télécharger (HTML)"):
            self.assertNotIn(absent, html)
        for valeur in attributs_href_action(html):
            self.assertNotIn(valeur.split("?")[0], ("/analyse", "/suivi", "/arreter", "/reprendre", "/api/etat",
                                                    "/tableau-de-bord", "/admin/rapports/60"), valeur)

    def test_rapport_identique_pour_l_administrateur(self):
        # Une seule vue par URL : seul le menu change pour l'administrateur (lien vers l'administration des campagnes).
        self.assertEqual(principal(self.html("/rapports/60")), principal(self.html("/rapports/60", auth=MOT_DE_PASSE)))
        menu = self.html("/rapports/60", auth=MOT_DE_PASSE)
        self.assertIn('href="/admin/campagnes"', menu)
        self.assertNotIn('href="/suivi"', menu)

    def test_csv_et_export_autonome_assainis(self):
        statut, en_tetes, corps = self.appel("GET", "/rapports/60/communes.csv")
        self.assertEqual(statut, 200)
        self.assertTrue(en_tetes.get("Content-Type", "").startswith("text/csv"))
        self.assertIn('filename="communes_60.csv"', en_tetes.get("Content-Disposition", ""))
        self.assertTrue(corps.startswith("\ufeffCode INSEE;Commune;Statut"))
        statut, en_tetes, corps = self.appel("GET", "/rapports/60/export.html")
        self.assertEqual(statut, 200)
        self.assertIn("filename=compte-rendu_60.html", en_tetes.get("Content-Disposition", ""))
        self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertIn("<!DOCTYPE html>", corps)
        for absent in ("Détails techniques", "Valeurs brutes", "Liens écartés", "/suivi", "/analyse", "/api/etat"):
            self.assertNotIn(absent, corps)

    def test_ancienne_adresse_redirigee(self):
        statut, en_tetes, _ = self.appel("GET", "/rapport/60")
        self.assertEqual((statut, en_tetes.get("Location")), (303, "/rapports/60"))
        self.assertEqual(self.appel("GET", "/rapport/99")[0], 404)

    def test_departement_non_publie_404(self):
        for chemin in ("/rapports/99", "/rapports/99/communes.csv", "/rapports/99/export.html"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 404)

    def test_head_rapport_sans_corps(self):
        statut, _, corps = self.appel("HEAD", "/rapports/60")
        self.assertEqual((statut, corps), (200, ""))
        self.assertEqual(self.appel("HEAD", "/rapports/60/export.html")[0], 200)


class SourceCampagnes(FileSource):
    """Source fichiers avec un historique d'analyses (la source fichiers n'en a pas) : une analyse par statut."""

    JOURNAL = "Journal secret : RUN-SECRET-42 lancé par bob@example.org"

    def __init__(self, root: Path):
        super().__init__(root)
        jour = self.resumes()[0]["date"][:10]  # date de publication du 60 : la fenêtre des analyses la couvre
        self.liste = [
            {"id": 1, "statut": "terminee", "departements": ["60"], "debut": f"{jour}T00:00:00+00:00",
             "fin": f"{jour}T23:59:59+00:00"},
            {"id": 2, "statut": "en_cours", "departements": ["60"], "debut": f"{jour}T00:00:00+00:00", "fin": None},
            {"id": 3, "statut": "echec", "departements": ["60"], "debut": f"{jour}T00:00:00+00:00",
             "fin": f"{jour}T23:59:59+00:00"},
            {"id": 4, "statut": "annulee", "departements": ["60"], "debut": f"{jour}T00:00:00+00:00",
             "fin": f"{jour}T23:59:59+00:00"},
            {"id": 5, "statut": "terminee", "departements": ["all"], "debut": f"{jour}T00:00:00+00:00",
             "fin": f"{jour}T23:59:59+00:00"},
        ]

    def analyses(self, limit: int = 50) -> list[dict]:
        return [dict(a) for a in self.liste][:limit]  # sans journal, comme store_pg.analyses

    def analyse(self, analysis_id: int) -> dict | None:
        for a in self.liste:
            if a["id"] == analysis_id:
                return {**a, "journal": self.JOURNAL if analysis_id == 1 else ""}
        return None


class LectureCampagnes(ServeurLecture):
    """Historique public : analyses terminées seulement, vue assainie ; vue administrateur sous /admin/."""

    lecture = True

    def creer_source(self):
        return SourceCampagnes(self.root)

    def test_historique_terminees_seulement(self):
        html = self.html("/campagnes")
        self.assertIn('href="/campagnes/1"', html)
        self.assertIn('href="/campagnes/5"', html)
        for n in (2, 3, 4):
            self.assertNotIn(f'href="/campagnes/{n}"', html)
        for mot in ("En cours", "Échec", "Interrompue", "RUN-SECRET", "bob@example.org", "Journal"):
            self.assertNotIn(mot, html)

    def test_detail_terminee_assainie(self):
        html = self.html("/campagnes/1")
        self.assertIn("<h1>Analyse n° 1</h1>", html)
        self.assertIn("Terminée", html)
        self.assertIn('href="/rapports/60"', html)
        self.assertIn("Communes des départements rattachés : 1", html)
        self.assertIn("Lien trouvé", html)                 # libellés de statut de commune
        for absent in ("Journal", "Journal secret", "RUN-SECRET", "bob@example.org", "/admin/", "/suivi", "/analyse"):
            self.assertNotIn(absent, html)

    def test_perimetre_toute_la_france_et_rattachement(self):
        html = self.html("/campagnes/5")
        self.assertIn("toute la France", html)
        self.assertIn("Communes des départements rattachés : 2", html)

    def test_campagne_non_terminee_introuvable(self):
        for n in (2, 3, 4, 6, 99):
            with self.subTest(n=n):
                statut, _, corps = self.appel("GET", f"/campagnes/{n}")
                self.assertEqual(statut, 404)
                self.assertNotIn("Échec", corps)
                self.assertNotIn("annul", corps.lower())

    def test_meme_vue_pour_l_administrateur(self):
        self.assertEqual(principal(self.html("/campagnes/1")), principal(self.html("/campagnes/1", auth=MOT_DE_PASSE)))
        self.assertEqual(principal(self.html("/campagnes", auth=MOT_DE_PASSE)), principal(self.html("/campagnes")))

    def test_vue_administrateur_des_campagnes(self):
        for chemin in ("/admin/campagnes", "/admin/campagnes/1", "/admin/rapports/60"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 401)
        liste = self.html("/admin/campagnes", auth=MOT_DE_PASSE)
        for n in (1, 2, 3, 4, 5):                          # l'administrateur voit toutes les analyses
            self.assertIn(f'href="/admin/campagnes/{n}"', liste)
        detail = self.html("/admin/campagnes/1", auth=MOT_DE_PASSE)
        self.assertIn("Journal secret", detail)            # journal et détails d'exécution : administrateur seul
        self.assertEqual(self.appel("GET", "/admin/campagnes/99", auth=MOT_DE_PASSE)[0], 404)
        self.assertIn("Détails techniques", self.html("/admin/rapports/60", auth=MOT_DE_PASSE))

    def test_numero_hors_borne_protege(self):
        self.assertEqual(self.appel("GET", "/campagnes/0")[0], 404)
        self.assertEqual(self.appel("GET", "/campagnes/1234567890")[0], 401)
        self.assertEqual(self.appel("GET", "/campagnes/1234567890", auth=MOT_DE_PASSE)[0], 404)

    def test_une_seule_titre_1_et_menu_admin(self):
        self.assertEqual(len(re.findall(r"<h1[\s>]", self.appel("GET", "/campagnes/1")[2])), 1)
        self.assertIn('href="/admin/campagnes"', self.html("/campagnes", auth=MOT_DE_PASSE))
        self.assertNotIn('href="/admin/campagnes"', self.html("/campagnes"))


if __name__ == "__main__":
    unittest.main()
