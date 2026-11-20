"""Accueil unique : chiffres clés, dernière analyse et bloc Rapports en haut de l'accueil, menus sans « Tableau de bord »,
ancienne adresse /tableau-de-bord redirigée, lecture publique désactivée = même contenu sous mot de passe.
Serveur réel sur 127.0.0.1, source fichiers. Aucun réseau.
"""
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_consultation as tc  # noqa: E402
from publications import FileSource  # noqa: E402

MOT_DE_PASSE = tc.MOT_DE_PASSE


def corps_principal(html: str) -> str:
    return html[html.index("<main"):html.index("</main>")]


class SourceEnCoursEnTete(tc.SourceCampagnes):
    """Analyses la plus récente en premier : une analyse en cours (9), puis un échec (8), puis une analyse terminée (7)."""

    def __init__(self, root: Path):
        super().__init__(root)
        jour = self.resumes()[0]["date"][:10]
        self.liste = [
            {"id": 9, "statut": "en_cours", "departements": ["60"], "debut": f"{jour}T12:00:00+00:00", "fin": None},
            {"id": 8, "statut": "echec", "departements": ["60"], "debut": f"{jour}T11:00:00+00:00",
             "fin": f"{jour}T11:30:00+00:00"},
            {"id": 7, "statut": "terminee", "departements": ["60"], "debut": f"{jour}T10:00:00+00:00",
             "fin": f"{jour}T10:30:00+00:00"},
        ]


class AccueilUnifie(tc.ServeurLecture):
    lecture = True

    def creer_source(self):
        return SourceEnCoursEnTete(self.root)

    def test_blocs_dans_l_ordre_apres_le_titre(self):
        corps = corps_principal(self.html("/"))
        self.assertEqual(corps.count("<h1"), 1)
        ordre = ["<h1>Résultats publiés</h1>", "Chiffres clés", "Dernière analyse", 'href="/rapports"',
                 "Départements publiés", "Nouveaux liens", "Tous les liens trouvés"]
        positions = [corps.index(marqueur) for marqueur in ordre]
        self.assertEqual(positions, sorted(positions))

    def test_chiffres_uniques_et_valeurs_du_bandeau(self):
        html = self.html("/")
        corps = corps_principal(html)
        resumes = FileSource(self.root).resumes()
        attendus = {"communes": sum(int(r["nb_communes"]) for r in resumes),
                    "communes avec un lien": sum(int(r["nb_communes_avec_lien"]) for r in resumes),
                    "communes à vérifier": sum(int(r["nb_communes_a_verifier"]) for r in resumes)}
        for libelle, valeur in attendus.items():
            with self.subTest(libelle=libelle):
                self.assertEqual(corps.count(f'<p class="fr-mb-0">{libelle}</p>'), 1)  # un seul ensemble, sans doublon
                self.assertRegex(corps, rf'<p class="fr-h2 fr-mb-1w">{valeur}</p><p class="fr-mb-0">{libelle}</p>')
        self.assertEqual(corps.count('<p class="fr-mb-0">départements publiés</p>'), 1)
        self.assertNotIn("Synthèse de toutes les publications", html)

    def test_visiteur_voit_la_derniere_analyse_terminee_sans_identifiant_d_execution(self):
        html = self.html("/")
        corps = corps_principal(html)
        self.assertIn('<a class="fr-link" href="/campagnes/7">n° 7</a>', corps)
        self.assertIn("Terminée", corps)
        for absent in ("n° 9", "/campagnes/9", "En cours", "Échec", "RUN-", "run_id", "Une analyse est en cours"):
            self.assertNotIn(absent, corps)

    def test_administrateur_voit_l_analyse_en_cours_sans_lien(self):
        corps = corps_principal(self.html("/", auth=MOT_DE_PASSE))
        self.assertIn("n° 9", corps)
        self.assertIn("En cours", corps)
        self.assertNotIn("/campagnes/9", corps)            # pas de lien vers une analyse non terminée
        self.assertNotIn('href="/suivi"', corps)           # ni lien vers le suivi, même pour l'administrateur

    def test_aucun_lien_de_lancement_ni_de_suivi_dans_le_corps(self):
        for auth in (None, MOT_DE_PASSE):
            with self.subTest(auth=auth):
                html = self.html("/", auth=auth)
                corps = corps_principal(html)
                for interdit in ('href="/analyse"', 'href="/suivi"', 'href="/arreter"', "Lancer une analyse",
                                 "Suivi de l'analyse", 'href="/tableau-de-bord"', 'href="/api/etat"'):
                    self.assertNotIn(interdit, corps)

    def test_menus_sans_tableau_de_bord_et_menu_admin_complet(self):
        for chemin, auth in (("/", None), ("/rapports", None), ("/a-propos", None), ("/", MOT_DE_PASSE),
                             ("/rapports/60", MOT_DE_PASSE)):
            with self.subTest(chemin=chemin, auth=auth):
                self.assertNotIn("Tableau de bord", self.html(chemin, auth=auth))
        menu_admin = self.html("/", auth=MOT_DE_PASSE)
        self.assertIn('href="/analyse"', menu_admin)
        self.assertIn('href="/suivi"', menu_admin)
        menu_visiteur = self.html("/")
        self.assertNotIn('href="/suivi"', menu_visiteur)
        self.assertIn('href="/rapports"', corps_principal(menu_visiteur))  # bloc Rapports dans le corps

    def test_bloc_rapports_renvoie_vers_rapports_publics(self):
        corps = corps_principal(self.html("/"))
        self.assertIn("Voir les rapports", corps)
        self.assertRegex(corps, r'<a class="fr-btn" href="/rapports">Voir les rapports</a>')

    def test_ancienne_adresse_redirigee_sans_contenu(self):
        for auth in (None, MOT_DE_PASSE):
            with self.subTest(auth=auth):
                statut, en_tetes, corps = self.appel("GET", "/tableau-de-bord", auth=auth)
                self.assertEqual((statut, en_tetes.get("Location"), corps), (308, "/", ""))


class AccueilProtegeeIdentique(tc.ServeurLecture):
    """Lecture publique désactivée : l'accueil protégé affiche le même contenu que l'accueil public."""

    lecture = False

    def creer_source(self):
        return tc.SourceCampagnes(self.root)  # analyse la plus récente terminée : même dernière analyse pour tous

    def _page_publique(self, chemin: str) -> str:
        serveur = tc.webapp.serve(self.root, "127.0.0.1", 0, MOT_DE_PASSE, self.runner, self.creer_source(),
                                  lecture_publique=True)
        self.addCleanup(serveur.server_close)
        threading.Thread(target=serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(serveur.shutdown)
        url = f"http://127.0.0.1:{serveur.server_address[1]}{chemin}"
        with tc.OPENER.open(urllib.request.Request(url), timeout=10) as r:
            return r.read().decode("utf-8")

    def test_protegee_sans_mot_de_passe_refusee_et_meme_contenu(self):
        self.assertEqual(self.appel("GET", "/")[0], 401)
        protegee = self.html("/", auth=MOT_DE_PASSE)
        self.assertEqual(corps_principal(protegee), corps_principal(self._page_publique("/")))
        self.assertIn("Chiffres clés", protegee)
        self.assertIn("Dernière analyse", protegee)
        self.assertNotIn('href="/tableau-de-bord"', protegee)
        self.assertIn('href="/suivi"', protegee)           # menu d'administration complet

    def test_ancienne_adresse_sans_identifiants(self):
        statut, en_tetes, corps = self.appel("GET", "/tableau-de-bord")
        self.assertEqual((statut, en_tetes.get("Location"), corps), (308, "/", ""))
