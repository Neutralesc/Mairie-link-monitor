"""Adaptateur WSGI de api/index.py (point d'entrée Vercel) : la requête d'origine doit arriver intacte au gestionnaire."""
import base64
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RACINE = Path(__file__).resolve().parent.parent


def charger(env: dict):
    with mock.patch.dict(os.environ, env, clear=True):
        spec = importlib.util.spec_from_file_location("api_index_wsgi_test", RACINE / "api" / "index.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def appeler(app, methode="GET", chemin="/", query="", entetes=None, corps=b""):
    environ = {"REQUEST_METHOD": methode, "PATH_INFO": chemin, "QUERY_STRING": query, "REMOTE_ADDR": "1.2.3.4",
               "wsgi.input": io.BytesIO(corps), "CONTENT_LENGTH": str(len(corps)) if corps else "",
               "HTTP_HOST": "exemple.vercel.app"}
    environ.update({"HTTP_" + k.upper().replace("-", "_"): v for k, v in (entetes or {}).items()})
    resultat = {}
    resultat["corps"] = b"".join(app(environ, lambda statut, en: resultat.update(statut=statut, entetes=dict(en))))
    return resultat


class AppWsgiTests(unittest.TestCase):
    def test_app_est_une_fonction_de_premier_niveau(self):
        # Vercel lit le fichier de façon statique : `app` doit être définie explicitement.
        source = (RACINE / "api" / "index.py").read_text(encoding="utf-8")
        self.assertIn("\ndef app(environ, start_response)", source)

    def test_pas_de_reecriture_vers_api_index(self):
        import json
        config = json.loads((RACINE / "vercel.json").read_text(encoding="utf-8"))
        self.assertNotIn("rewrites", config)       # le mode framework route avec le chemin d'origine

    def test_configuration_incomplete_503_sur_tout_chemin(self):
        m = charger({})
        for chemin in ("/", "/rapport/60", "/csv/tous", "/nimporte/quoi"):
            r = appeler(m.app, chemin=chemin)
            self.assertTrue(r["statut"].startswith("503"), chemin)
            self.assertIn(b"DATABASE_URL", r["corps"])

    def test_chemin_d_origine_et_authentification(self):
        m = charger({"DATABASE_URL": "postgresql://u@hote-inexistant.invalid:1/db", "MLM_PASSWORD": "pw",
                     "MLM_GH_TOKEN": "jeton-secret", "MLM_GH_REPO": "o/r"})
        self.assertTrue(appeler(m.app)["statut"].startswith("401"))
        auth = {"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()}
        r = appeler(m.app, entetes=auth)
        self.assertTrue(r["statut"].startswith("200"))                          # base injoignable : accueil quand même
        self.assertNotIn(b"jeton-secret", r["corps"])
        self.assertTrue(appeler(m.app, chemin="/inconnu", query="x=a%20b", entetes=auth)["statut"].startswith("404"))
        self.assertEqual(r["entetes"].get("X-Frame-Options"), "DENY")

    def test_post_d_une_autre_origine_refuse(self):
        m = charger({"DATABASE_URL": "postgresql://u@hote-inexistant.invalid:1/db", "MLM_PASSWORD": "pw",
                     "MLM_GH_TOKEN": "t", "MLM_GH_REPO": "o/r"})
        auth = {"Authorization": "Basic " + base64.b64encode(b"u:pw").decode(), "Origin": "http://evil.example",
                "Content-Type": "application/x-www-form-urlencoded"}
        r = appeler(m.app, methode="POST", chemin="/lancer", entetes=auth, corps=b"dept=60")
        self.assertTrue(r["statut"].startswith("403"))


if __name__ == "__main__":
    unittest.main()
