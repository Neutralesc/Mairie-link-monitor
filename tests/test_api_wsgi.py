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


PRINCIPAL = "mairie-link-monitor.vercel.app"
ANCIEN = "mairie-link-monitor-neutralex.vercel.app"
REDIRECTION = {"MLM_HOTE_PRINCIPAL": PRINCIPAL, "MLM_HOTES_REDIRIGES": ANCIEN}
CONFIG_INCOMPLETE_OK = {"DATABASE_URL": "postgresql://u@hote-inexistant.invalid:1/db", "MLM_PASSWORD": "pw",
                        "MLM_GH_TOKEN": "t", "MLM_GH_REPO": "o/r"}


class RedirectionHoteTests(unittest.TestCase):
    def app_avec(self, **surcharges):
        env = {**CONFIG_INCOMPLETE_OK, **REDIRECTION, **surcharges}
        return charger({k: v for k, v in env.items() if v is not None}).app

    def test_hote_liste_308_avec_chemin_et_requete(self):
        r = appeler(self.app_avec(), chemin="/resultats", query="dept=60&q=a%20b", entetes={"Host": ANCIEN})
        self.assertEqual(r["statut"], "308 Permanent Redirect")
        self.assertEqual(r["entetes"]["Location"], f"https://{PRINCIPAL}/resultats?dept=60&q=a%20b")
        self.assertEqual(r["entetes"]["Cache-Control"], "no-store")
        self.assertEqual(r["corps"], b"")

    def test_redirection_avant_authentification(self):
        r = appeler(self.app_avec(), chemin="/", entetes={"Host": ANCIEN})
        self.assertEqual(r["statut"], "308 Permanent Redirect")
        self.assertEqual(r["entetes"]["Location"], f"https://{PRINCIPAL}/")

    def test_methode_conservee(self):
        r = appeler(self.app_avec(), methode="POST", chemin="/lancer", entetes={"Host": ANCIEN}, corps=b"dept=60")
        self.assertEqual(r["statut"], "308 Permanent Redirect")
        self.assertEqual(r["entetes"]["Location"], f"https://{PRINCIPAL}/lancer")

    def test_hote_principal_pas_de_redirection(self):
        r = appeler(self.app_avec(), chemin="/", entetes={"Host": PRINCIPAL})
        self.assertEqual(r["statut"], "401 Unauthorized")  # comportement normal : authentification exigée

    def test_hote_d_apercu_inchange(self):
        apercu = "mairie-link-monitor-git-essai-neutralex.vercel.app"
        r = appeler(self.app_avec(), chemin="/", entetes={"Host": apercu})
        self.assertEqual(r["statut"], "401 Unauthorized")
        self.assertNotIn("Location", r["entetes"])

    def test_sans_variables_inchange(self):
        auth = {"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()}
        for env in ({}, {"MLM_HOTE_PRINCIPAL": "", "MLM_HOTES_REDIRIGES": ANCIEN}, {"MLM_HOTES_REDIRIGES": "  ,  "}):
            with self.subTest(env=env):
                r = appeler(charger({**CONFIG_INCOMPLETE_OK, **env}).app, chemin="/",
                            entetes={"Host": ANCIEN, **auth})
                self.assertEqual(r["statut"], "200 OK")  # config complète : accueil (base injoignable, alerte)
                self.assertNotIn("Location", r["entetes"])

    def test_principal_sans_liste_avertit_sans_redirection(self):
        with self.assertLogs("api", level="WARNING"):
            m = charger({**CONFIG_INCOMPLETE_OK, "MLM_HOTE_PRINCIPAL": PRINCIPAL})
        self.assertIsNone(m.REDIRECTION)
        self.assertEqual(appeler(m.app, chemin="/", entetes={"Host": ANCIEN})["statut"], "401 Unauthorized")

    def test_sans_variables_de_redirection_sans_effet_sur_503(self):
        r = appeler(charger({}).app, chemin="/", entetes={"Host": ANCIEN})
        self.assertTrue(r["statut"].startswith("503"))

    def test_chemins_suspects_restent_sur_l_hote_principal(self):
        app = self.app_avec()
        for chemin in ("//evil.example", "//evil.example/x", "\\\\evil.example", "/\\evil.example", "/a\r\nSet-Cookie: x=1"):
            with self.subTest(chemin=chemin):
                r = appeler(app, chemin=chemin, entetes={"Host": ANCIEN})
                self.assertEqual(r["statut"], "308 Permanent Redirect")
                self.assertTrue(r["entetes"]["Location"].startswith(f"https://{PRINCIPAL}/"))
                self.assertNotIn("evil", r["entetes"]["Location"])
                self.assertNotIn("\r", r["entetes"]["Location"])
                self.assertNotIn("\n", r["entetes"]["Location"])

    def test_port_et_majuscules_dans_host(self):
        app = self.app_avec()
        for hote in (f"{ANCIEN.upper()}:443", f"{ANCIEN}.", f"  {ANCIEN.upper()}  "):
            with self.subTest(hote=hote):
                r = appeler(app, chemin="/a", entetes={"Host": hote})
                self.assertEqual(r["statut"], "308 Permanent Redirect")
                self.assertEqual(r["entetes"]["Location"], f"https://{PRINCIPAL}/a")

    def test_x_forwarded_host_premier_element(self):
        app = self.app_avec()
        r = appeler(app, chemin="/a", entetes={"Host": PRINCIPAL, "X-Forwarded-Host": f"{ANCIEN}, {PRINCIPAL}"})
        self.assertEqual(r["statut"], "308 Permanent Redirect")
        r = appeler(app, chemin="/a", entetes={"Host": ANCIEN, "X-Forwarded-Host": f"{PRINCIPAL}, {ANCIEN}"})
        self.assertEqual(r["statut"], "401 Unauthorized")

    def test_variables_invalides_ignorees_avec_avertissement(self):
        with self.assertLogs("api", level="WARNING") as logs:
            m = charger({**CONFIG_INCOMPLETE_OK, "MLM_HOTE_PRINCIPAL": "https://mairie.example/",
                         "MLM_HOTES_REDIRIGES": ANCIEN})
        self.assertIsNone(m.REDIRECTION)                       # principal invalide : aucune redirection
        self.assertIn("MLM_HOTE_PRINCIPAL invalide", "\n".join(logs.output))
        self.assertNotIn("https://mairie.example", "\n".join(logs.output))
        with self.assertLogs("api", level="WARNING"):
            m = charger({**CONFIG_INCOMPLETE_OK, "MLM_HOTE_PRINCIPAL": PRINCIPAL,
                         "MLM_HOTES_REDIRIGES": f"{ANCIEN}, mauvais hote/x, autre.example"})
        self.assertEqual(m.REDIRECTION, (PRINCIPAL, frozenset({ANCIEN, "autre.example"})))  # l'invalide seule est ignorée
        r = appeler(m.app, chemin="/", entetes={"Host": "mauvais hote/x"})
        self.assertEqual(r["statut"], "401 Unauthorized")

    def test_principal_jamais_dans_la_liste_redirigee(self):
        with self.assertLogs("api", level="WARNING"):  # liste réduite à l'hôte principal : aucune redirection signalée
            m = charger({**CONFIG_INCOMPLETE_OK, "MLM_HOTE_PRINCIPAL": PRINCIPAL, "MLM_HOTES_REDIRIGES": PRINCIPAL})
        self.assertIsNone(m.REDIRECTION)


if __name__ == "__main__":
    unittest.main()
