"""Point d'entrée Vercel (api/index.py) : onglet « Résultats » de bout en bout, et panne de base pour cette page.

Le module api/index.py est chargé avec un environnement contrôlé (charger_handler). Les cas Postgres sont ignorés sans
MLM_TEST_DATABASE_URL ; les cas de panne utilisent une URL invalide (échec immédiat, sans réseau). Aucun formulaire
n'est envoyé : une analyse appellerait GitHub.
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
import support_publications as sp  # noqa: E402  (chemins src/ et tests/)
import store_pg  # noqa: E402
from publications import FileSource  # noqa: E402

# Erreurs de base attendues : journalisées sans bruit sur stderr (assertLogs capture quand il est utilisé).
for _nom in ("api", "actions_runner", "webapp"):
    logging.getLogger(_nom).addHandler(logging.NullHandler())

API = sp.RACINE / "api" / "index.py"
MOT_DE_PASSE = "mot-de-passe-resultats-api-5b"
JETON = "jeton-github-resultats-api-2d"
DEPOT = "proprio/depot"
URL_PANNE = "://url-invalide-de-test"
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
        spec = importlib.util.spec_from_file_location("api_index_resultats_de_test", API)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.handler


class ServeurApi(unittest.TestCase):
    def demarrer(self, env: dict) -> None:
        serveur = HTTPServer(("127.0.0.1", 0), charger_handler(env))
        self.addCleanup(serveur.server_close)
        self.addCleanup(serveur.shutdown)
        threading.Thread(target=serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{serveur.server_address[1]}"

    def appel(self, chemin: str, auth: str | None = None):
        """(statut, en-têtes, corps texte). Les réponses d'erreur HTTP ne lèvent pas d'exception."""
        req = urllib.request.Request(self.base + chemin)
        if auth is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{auth}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class ApiResultatsPostgresTests(ServeurApi, sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        racine = Path(tmp.name)
        sp.construire_racine(racine)                 # 60 : commune 60001 « A » avec lien ; 80 : sans lien
        conn = store_pg.connect(self.url)
        try:
            store_pg.publish(FileSource(racine), conn)
        finally:
            conn.close()
        self.demarrer(configuration(DATABASE_URL=self.url))

    def test_resultats_avec_basic_liste_la_commune_du_60(self):
        status, _, html = self.appel("/resultats", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("<h1>Résultats</h1>", html)
        self.assertIn("<td>60</td><td>A</td><td>60001</td>", html)
        self.assertIn("<strong>1 commune avec lien</strong>", html)
        self.assertIn("<caption>Communes dont le site contient un lien vers Vos Questions</caption>", html)
        self.assertIn('aria-current="page">Résultats</a>', html)

    def test_resultats_filtre_et_recherche_depuis_postgres(self):
        status, _, html = self.appel("/resultats?dept=60&q=60001", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("<td>A</td>", html)
        status, _, html = self.appel("/resultats?dept=60&q=zzzz", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("Aucune commune ne correspond à votre recherche.", html)

    def test_resultats_sans_authentification_401(self):
        status, en_tetes, corps = self.appel("/resultats")
        self.assertEqual(status, 401)
        self.assertIn("Basic", en_tetes.get("WWW-Authenticate", ""))
        self.assertNotIn("<h1>Résultats</h1>", corps)
        self.assertNotIn(MOT_DE_PASSE, corps)

    def test_accueil_avec_basic_navigation_accueil_active(self):
        status, _, corps = self.appel("/", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn('href="/" target="_self" aria-current="page">Accueil</a>', corps)
        self.assertIn('href="/resultats" target="_self">Résultats</a>', corps)
        self.assertIn("Résultats publiés", corps)


class ApiResultatsBasePanneTests(ServeurApi):
    """Configuration complète, base injoignable : aucune base réelle requise."""

    def setUp(self):
        self.demarrer(configuration())

    def test_resultats_sans_authentification_401_avant_la_base(self):
        self.assertEqual(self.appel("/resultats")[0], 401)

    def test_resultats_503_base_en_panne(self):
        with self.assertLogs("api", level="WARNING") as logs:
            status, _, corps = self.appel("/resultats", auth=MOT_DE_PASSE)
        self.assertEqual(status, 503)
        self.assertIn("Service indisponible", corps)
        self.assertNotIn("<h1>Résultats</h1>", corps)
        texte = "\n".join(logs.output)
        self.assertNotIn(URL_PANNE, texte)
        self.assertNotIn(URL_PANNE, corps)

    def test_accueil_200_base_en_panne(self):
        with self.assertLogs("api", level="WARNING"):
            status, _, corps = self.appel("/", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("Lancer une analyse", corps)
        self.assertIn("Base de données indisponible", corps)
        self.assertIn('href="/resultats" target="_self">Résultats</a>', corps)   # menu toujours présent


if __name__ == "__main__":
    unittest.main()
