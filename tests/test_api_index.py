"""Point d'entrée Vercel (api/index.py) : configuration, authentification, lecture Postgres, pannes. Sans réseau externe.

Le module est rechargé à chaque cas, avec des variables d'environnement contrôlées. Les cas PostgreSQL sont ignorés sans
MLM_TEST_DATABASE_URL ; les autres tournent toujours (base volontairement injoignable pour la panne).
Aucun formulaire n'est envoyé avec un mot de passe valide : une analyse déclencherait un appel à GitHub.
"""
import base64
import importlib.util
import logging
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
import store_pg  # noqa: E402
from publications import FileSource  # noqa: E402

# Pannes de base attendues : journalisées sans être affichées sur stderr (assertLogs capture quand il est utilisé).
for _nom in ("api", "actions_runner", "webapp"):
    logging.getLogger(_nom).addHandler(logging.NullHandler())

API = sp.RACINE / "api" / "index.py"
MOT_DE_PASSE = "mot-de-passe-test-7f3a"
JETON = "jeton-github-test-9c1e"
DEPOT = "proprio/depot"
URL_PANNE = "://url-invalide-de-test"  # échoue à la connexion immédiatement, sans réseau
OBLIGATOIRES = ("DATABASE_URL", "MLM_PASSWORD", "MLM_GH_TOKEN", "MLM_GH_REPO")


def configuration(**surcharges) -> dict:
    """Environnement complet (base injoignable par défaut) ; une valeur None retire la variable."""
    env = {"DATABASE_URL": URL_PANNE, "MLM_PASSWORD": MOT_DE_PASSE, "MLM_GH_TOKEN": JETON, "MLM_GH_REPO": DEPOT}
    for nom, valeur in surcharges.items():
        if valeur is None:
            env.pop(nom, None)
        else:
            env[nom] = valeur
    return env


def charger_handler(env: dict):
    """Exécute api/index.py avec exactement `env` comme environnement et retourne la classe `handler`."""
    with mock.patch.dict(os.environ, env, clear=True):
        spec = importlib.util.spec_from_file_location("api_index_de_test", API)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.handler


class ServeurApi(unittest.TestCase):
    def demarrer(self, env: dict) -> None:
        handler = charger_handler(env)
        self.serveur = HTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self._arreter)
        threading.Thread(target=self.serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.serveur.server_address[1]}"

    def _arreter(self) -> None:
        self.serveur.shutdown()
        self.serveur.server_close()

    def appel(self, chemin: str, auth: str | None = None, data: bytes | None = None):
        """Retourne (statut, en-têtes, corps en texte). Les réponses d'erreur HTTP ne lèvent pas d'exception."""
        req = urllib.request.Request(self.base + chemin, data=data)
        if auth is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{auth}".encode()).decode())
        if data is not None:
            req.add_header("Origin", self.base)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")


class ConfigurationIncompleteTests(ServeurApi):
    def test_sans_variables_503_avec_les_noms_seulement(self):
        self.demarrer({})
        status, _, corps = self.appel("/")
        self.assertEqual(status, 503)
        self.assertIn("Configuration incomplète", corps)
        for nom in OBLIGATOIRES:
            self.assertIn(nom, corps)

    def test_503_aussi_pour_les_formulaires(self):
        self.demarrer({})
        self.assertEqual(self.appel("/lancer", data=b"dept=60")[0], 503)

    def test_valeurs_jamais_affichees(self):
        self.demarrer(configuration(DATABASE_URL=None))
        status, _, corps = self.appel("/")
        self.assertEqual(status, 503)
        self.assertIn("DATABASE_URL", corps)
        self.assertNotIn("MLM_PASSWORD", corps)                 # présente : ne doit pas être listée
        for secret in (MOT_DE_PASSE, JETON, URL_PANNE):
            self.assertNotIn(secret, corps)

    def test_mot_de_passe_vide_503_jamais_servi(self):
        self.demarrer(configuration(MLM_PASSWORD="   "))
        status, _, corps = self.appel("/")
        self.assertEqual(status, 503)                           # ni 200 ni 401 : rien n'est servi
        self.assertIn("MLM_PASSWORD", corps)
        self.assertEqual(self.appel("/", auth="n-importe-quoi")[0], 503)

    def test_depot_invalide_503_sans_secret(self):
        self.demarrer(configuration(MLM_GH_REPO="sans-barre-oblique"))
        status, _, corps = self.appel("/")
        self.assertEqual(status, 503)
        self.assertIn("Configuration invalide", corps)
        self.assertNotIn(JETON, corps)
        self.assertNotIn(MOT_DE_PASSE, corps)


class ApiBaseEnPanneTests(ServeurApi):
    """Configuration complète, base injoignable : aucune base réelle requise."""

    def setUp(self):
        self.demarrer(configuration())

    def test_authentification_exigee_avant_la_base(self):
        self.assertEqual(self.appel("/")[0], 401)
        self.assertEqual(self.appel("/", auth="faux")[0], 401)
        status, en_tetes, corps = self.appel("/")
        self.assertIn("Basic", en_tetes.get("WWW-Authenticate", ""))
        self.assertNotIn(MOT_DE_PASSE, corps)

    def test_accueil_reste_utilisable(self):
        with self.assertLogs("api", level="WARNING") as logs:
            status, _, corps = self.appel("/", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("Lancer une analyse", corps)
        self.assertIn("Base de données indisponible", corps)    # alerte à la place de la liste des départements
        self.assertNotIn("Aucun département analysé", corps)
        texte = "\n".join(logs.output)
        self.assertIn("base de données indisponible", texte)
        self.assertNotIn(URL_PANNE, texte)                      # l'URL n'est jamais journalisée

    def test_rapport_et_csv_tous_503_base_en_panne(self):
        with self.assertLogs("api", level="WARNING"):
            status, _, corps = self.appel("/rapport/60", auth=MOT_DE_PASSE)
        self.assertEqual(status, 503)
        self.assertIn("Service indisponible", corps)
        with self.assertLogs("api", level="WARNING"):
            status, _, corps = self.appel("/csv/tous", auth=MOT_DE_PASSE)
        self.assertEqual(status, 503)
        self.assertIn("Service indisponible", corps)

    def test_ressource_dsfr_sans_authentification(self):
        status, en_tetes, corps = self.appel("/static/dsfr/dsfr/dsfr.min.css")
        self.assertEqual(status, 200)
        self.assertEqual(en_tetes.get("Content-Type"), "text/css; charset=utf-8")
        self.assertGreater(len(corps), 1000)

    def test_chemin_statique_suspect_refuse(self):
        self.assertEqual(self.appel("/static/dsfr/../../README.md")[0], 404)
        self.assertEqual(self.appel("/static/dsfr/dsfr/absent.css")[0], 404)

    def test_formulaire_sans_authentification_refuse(self):
        self.assertEqual(self.appel("/lancer", data=b"dept=60")[0], 401)


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class ApiPostgresTests(ServeurApi, sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.racine = Path(tmp.name)
        sp.construire_racine(self.racine)
        conn = store_pg.connect(self.url)
        try:
            store_pg.publish(FileSource(self.racine), conn)
        finally:
            conn.close()
        self.demarrer(configuration(DATABASE_URL=self.url))

    def test_sans_authentification_401(self):
        status, en_tetes, corps = self.appel("/")
        self.assertEqual(status, 401)
        self.assertIn("Basic", en_tetes.get("WWW-Authenticate", ""))
        self.assertNotIn(MOT_DE_PASSE, corps)

    def test_avec_basic_accueil_lit_postgres(self):
        status, _, corps = self.appel("/", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("Lancer une analyse", corps)
        self.assertIn("Résultats par département", corps)
        self.assertIn('href="/rapport/60"', corps)              # publié dans le schéma de test
        self.assertIn('href="/csv/tous"', corps)                 # CSV global publié

    def test_rapport_et_csv_depuis_postgres(self):
        status, _, corps = self.appel("/rapport/60", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("Compte rendu 60", corps)
        status, _, octets = self.appel("/csv/60", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertEqual(octets.encode("utf-8"), FileSource(self.racine).get("60").csv_bytes)
        self.assertEqual(self.appel("/csv/tous", auth=MOT_DE_PASSE)[2].encode("utf-8"), sp.CSV_TOUS_OCTETS)

    def test_ressource_dsfr_sans_authentification(self):
        self.assertEqual(self.appel("/static/dsfr/dsfr/dsfr.min.css")[0], 200)

    def test_secrets_absents_des_reponses(self):
        reponses = [self.appel("/"), self.appel("/", auth="faux"), self.appel("/", auth=MOT_DE_PASSE),
                    self.appel("/rapport/60", auth=MOT_DE_PASSE), self.appel("/csv/60", auth=MOT_DE_PASSE),
                    self.appel("/lancer", data=b"dept=60")]
        for status, en_tetes, corps in reponses:
            with self.subTest(status=status):
                texte = corps + "\n".join(f"{k}: {v}" for k, v in en_tetes.items())
                for secret in (MOT_DE_PASSE, JETON, "pgtest@localhost"):
                    self.assertNotIn(secret, texte)


class ApiBaseEnPanneLotBTests(ServeurApi):
    """Base injoignable (correctif lot B) : pages de données en 503, accueil avec alerte, source fichiers sans panne."""

    def setUp(self):
        self.demarrer(configuration())

    def test_csv_60_503_base_en_panne(self):
        with self.assertLogs("api", level="WARNING") as logs:
            status, _, corps = self.appel("/csv/60", auth=MOT_DE_PASSE)
        self.assertEqual(status, 503)
        self.assertIn("Service indisponible", corps)
        self.assertNotIn(URL_PANNE, "\n".join(logs.output))

    def test_accueil_affiche_alerte_avertissement_et_pas_d_erreur(self):
        with self.assertLogs("api", level="WARNING"):
            status, _, corps = self.appel("/", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("fr-alert--warning", corps)
        self.assertNotIn("fr-alert--error", corps)

    def test_source_fichiers_ne_leve_jamais_source_indisponible(self):
        # Les appels ne doivent lever aucune exception (SourceIndisponible ou autre), racine vide puis remplie.
        with tempfile.TemporaryDirectory() as tmp:
            racine = Path(tmp)
            source = FileSource(racine)
            self.assertEqual(source.depts(), [])
            self.assertIsNone(source.get("60"))
            self.assertIsNone(source.csv_all())
            self.assertFalse(source.has_csv_all())
            sp.construire_racine(racine)
            source = FileSource(racine)
            self.assertEqual(source.depts(), ["02", "60", "80"])
            self.assertIsNotNone(source.get("60"))
            self.assertTrue(source.has_csv_all())


if __name__ == "__main__":
    unittest.main()
