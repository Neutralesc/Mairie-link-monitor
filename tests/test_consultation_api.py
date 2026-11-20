"""Activation de l'espace consultation par MLM_LECTURE_PUBLIQUE dans api/index.py (point d'entrée Vercel).

Le module est rechargé à chaque cas avec un environnement contrôlé. Base volontairement injoignable : aucun accès réseau
ni base réelle requis. Aucun formulaire n'est envoyé avec un mot de passe valide.
"""
import base64
import importlib.util
import logging
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402,F401  (chemins de test)

for _nom in ("api", "actions_runner", "webapp"):
    logging.getLogger(_nom).addHandler(logging.NullHandler())

API = sp.RACINE / "api" / "index.py"
MOT_DE_PASSE = "mot-de-passe-api-consultation-9d1c"
JETON = "jeton-github-consultation-3e7a"
URL_PANNE = "://url-invalide-de-consultation"  # échoue à la connexion immédiatement, sans réseau
OBLIGATOIRES = ("DATABASE_URL", "MLM_PASSWORD", "MLM_GH_TOKEN", "MLM_GH_REPO")


def configuration(**surcharges) -> dict:
    env = {"DATABASE_URL": URL_PANNE, "MLM_PASSWORD": MOT_DE_PASSE, "MLM_GH_TOKEN": JETON, "MLM_GH_REPO": "proprio/depot"}
    for nom, valeur in surcharges.items():
        if valeur is None:
            env.pop(nom, None)
        else:
            env[nom] = valeur
    return env


def charger_module(env: dict):
    with mock.patch.dict(os.environ, env, clear=True):
        spec = importlib.util.spec_from_file_location("api_index_consultation_test", API)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class ServeurApi(unittest.TestCase):
    def demarrer(self, env: dict) -> None:
        self.serveur = HTTPServer(("127.0.0.1", 0), charger_module(env).handler)
        self.addCleanup(self._arreter)
        threading.Thread(target=self.serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.serveur.server_address[1]}"

    def _arreter(self) -> None:
        self.serveur.shutdown()
        self.serveur.server_close()

    def appel(self, chemin: str, methode: str = "GET", auth: str | None = None, origine: bool = False,
              corps: bytes | None = None):
        en_tetes = {}
        if auth is not None:
            en_tetes["Authorization"] = "Basic " + base64.b64encode(f"x:{auth}".encode()).decode()
        if origine:
            en_tetes["Origin"] = self.base
        req = urllib.request.Request(self.base + chemin, data=corps, method=methode, headers=en_tetes)
        try:
            with urllib.request.build_opener(_SansRedirection).open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")


class ActivationParVariableTests(unittest.TestCase):
    def test_variable_non_obligatoire(self):
        self.assertNotIn("MLM_LECTURE_PUBLIQUE", charger_module(configuration()).OBLIGATOIRES)
        self.assertEqual(charger_module(configuration()).OBLIGATOIRES, OBLIGATOIRES)

    def test_valeurs_qui_activent(self):
        for valeur in ("1", "true", "oui", "TRUE", " Oui "):
            with self.subTest(valeur=valeur):
                self.assertTrue(_activee(configuration(MLM_LECTURE_PUBLIQUE=valeur)))

    def test_autres_valeurs_et_absence_desactivent(self):
        for valeur in (None, "", "0", "non", "false", "yes", "2", "  "):
            with self.subTest(valeur=valeur):
                self.assertFalse(_activee(configuration(MLM_LECTURE_PUBLIQUE=valeur)))


def _activee(env: dict) -> bool:
    """Vrai si //a-propos répond 200 sans authentification (la base injoignable n'empêche pas cette page)."""
    serveur = HTTPServer(("127.0.0.1", 0), charger_module(env).handler)
    try:
        threading.Thread(target=serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        url = f"http://127.0.0.1:{serveur.server_address[1]}//a-propos"
        with urllib.request.build_opener(_SansRedirection).open(url, timeout=10) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    finally:
        serveur.shutdown()
        serveur.server_close()


class LectureDesactiveeTests(ServeurApi):
    def setUp(self):
        self.demarrer(configuration(MLM_LECTURE_PUBLIQUE=None))

    def test_pages_publiques_derriere_le_mot_de_passe(self):
        for chemin in ("/a-propos", "/departement/60", "/departement/60.csv"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel(chemin)[0], 401)
                self.assertEqual(self.appel(chemin, auth=MOT_DE_PASSE)[0], 404)

    def test_accueil_protege(self):
        self.assertEqual(self.appel("/")[0], 401)


class LectureActiveeTests(ServeurApi):
    def setUp(self):
        self.demarrer(configuration(MLM_LECTURE_PUBLIQUE="1"))

    def test_consultation_sans_mot_de_passe_et_base_en_panne(self):
        status, en_tetes, corps = self.appel("/a-propos")
        self.assertEqual(status, 200)
        self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertIn("Date de la dernière publication indisponible", corps)
        status, _, corps = self.appel("/")  # résultats lus en base : panne -> 503 sobre, sans 401
        self.assertEqual(status, 503)
        self.assertIn("ne sont pas disponibles", corps)
        for secret in (MOT_DE_PASSE, JETON, URL_PANNE, "DATABASE_URL"):
            self.assertNotIn(secret, corps)

    def test_protegees_toujours_401(self):
        for chemin in ("/analyse", "/suivi", "/api/etat", "/rapports", "/campagnes", "/inconnu"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel(chemin)[0], 401)

    def test_racine_admin_avec_identifiant_valide(self):
        self.assertEqual(self.appel("/", auth=MOT_DE_PASSE)[0], 200)  # tableau de bord, alerte base incluse
        self.assertEqual(self.appel("/", auth="mauvais")[0], 503)  # identifiant invalide : visiteur, pas de 401

    def test_post_consultation_405_meme_avec_origine_valide(self):
        status, en_tetes, _ = self.appel("/departement/60", methode="POST", origine=True, corps=b"dept=60")
        self.assertEqual(status, 405)
        self.assertEqual(en_tetes.get("Allow"), "GET, HEAD")
        self.assertEqual(self.appel("/lancer", methode="POST", origine=True, corps=b"dept=60")[0], 401)

    def test_secrets_absents_des_reponses_consultation(self):
        for status, en_tetes, corps in (self.appel("/"), self.appel("/a-propos"),
                                        self.appel("/departement/60.csv")):
            texte = corps + "\n".join(f"{k}: {v}" for k, v in en_tetes.items())
            for secret in (MOT_DE_PASSE, JETON, URL_PANNE):
                self.assertNotIn(secret, texte)


class ConfigurationIncompleteTests(ServeurApi):
    def test_sans_mot_de_passe_la_consultation_n_est_jamais_servie(self):
        self.demarrer(configuration(MLM_PASSWORD=None, MLM_LECTURE_PUBLIQUE="1"))
        status, _, corps = self.appel("/a-propos")
        self.assertEqual(status, 503)
        self.assertIn("MLM_PASSWORD", corps)
        self.assertNotIn("Limites", corps)


if __name__ == "__main__":
    unittest.main()
