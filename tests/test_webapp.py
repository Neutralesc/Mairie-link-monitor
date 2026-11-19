"""Application web : pages, lancement et arrêt d'une analyse, mot de passe, protection des formulaires."""
import base64
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
import common  # noqa: E402,F401
import csvio  # noqa: E402
import history  # noqa: E402
import webapp  # noqa: E402
from layout import DeptPaths  # noqa: E402

FAKE = "import sys, time; print('ARGS', ' '.join(sys.argv[1:]), flush=True); time.sleep(20)"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class WebTests(unittest.TestCase):
    password = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runner = webapp.Runner(self.root, command=lambda args: [sys.executable, "-c", FAKE, *args])
        self.server = webapp.serve(self.root, "127.0.0.1", 0, self.password, self.runner)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.opener = urllib.request.build_opener(NoRedirect)

    def tearDown(self):
        self.runner.stop()
        if self.runner.proc:
            self.runner.proc.wait(5)
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, form=None, origin=True, auth=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(self.base + path, data=data)
        if form is not None and origin:
            req.add_header("Origin", self.base)
        if auth is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{auth}".encode()).decode())
        try:
            with self.opener.open(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def wait_log(self, text):
        for _ in range(50):
            if text in self.runner.tail():
                return
            time.sleep(0.1)
        self.fail(f"journal sans « {text} » : {self.runner.tail()!r}")


class LocalTests(WebTests):
    def test_accueil(self):
        status, html = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn("Lancer une analyse", html)
        self.assertIn('lang="fr"', html)
        self.assertIn('<label for="dept">', html)
        self.assertIn("Aucun département analysé", html)

    def test_lancer_suivre_arreter(self):
        status, _ = self.call("/lancer", {"dept": "60 1", "reprise": "1"})
        self.assertEqual(status, 303)
        self.wait_log("ARGS --dept 60 01 --resume")
        status, html = self.call("/")
        self.assertIn("Analyse en cours : 60 01 (reprise)", html)
        self.assertNotIn('http-equiv="refresh"', html)                          # RGAA 13.1 : pas de rechargement imposé
        self.assertIn('id="actualiser-auto"', html)
        self.assertEqual(self.call("/lancer", {"dept": "80"})[0], 409)          # une seule analyse à la fois
        self.assertEqual(self.call("/arreter", {})[0], 303)
        self.runner.proc.wait(5)
        self.assertNotIn("Analyse en cours", self.call("/")[1])

    def test_saisies_refusees(self):
        self.assertEqual(self.call("/lancer", {"dept": "60; rm -rf /"})[0], 400)
        self.assertEqual(self.call("/lancer", {"dept": ""})[0], 400)
        status, html = self.call("/lancer", {"dept": "tous"})
        self.assertEqual(status, 400)
        self.assertIn("cochez la case", html)
        self.assertIsNone(self.runner.proc)
        self.assertEqual(self.call("/lancer", {"dept": "tous", "confirmer_tous": "1"})[0], 303)
        self.wait_log("ARGS --dept all --confirm-all")

    def test_formulaire_venant_d_un_autre_site_refuse(self):
        self.assertEqual(self.call("/lancer", {"dept": "60"}, origin=False)[0], 403)
        req = urllib.request.Request(self.base + "/lancer", data=b"dept=60", headers={"Origin": "https://pirate.example"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.opener.open(req, timeout=5)
        self.assertEqual(e.exception.code, 403)
        self.assertIsNone(self.runner.proc)

    def test_resultats_compte_rendu_et_csv(self):
        p = DeptPaths(self.root, "60")
        p.reports_dir.mkdir(parents=True)
        csvio.write_rows(p.inventory_communes, [{"code_insee": "60001", "nom_commune": "A", "nb_urls_valides": "1"}])
        csvio.write_rows(p.links_found, [{"code_insee": "60001", "site_url": "https://a.fr/", "page_url": "https://a.fr/",
                                          "type_lien": "lien"}])
        csvio.write_rows(p.result_csv, [{"code_insee": "60001", "url_page": "https://a.fr/"}])
        history.record(p, history.db_path(self.root))
        html = self.call("/")[1]
        self.assertIn('href="/rapport/60"', html)
        self.assertIn('href="/csv/60"', html)
        status, html = self.call("/rapport/60")
        self.assertEqual(status, 200)
        self.assertIn("Communes avec un lien (1)", html)
        status, body = self.call("/csv/60")
        self.assertEqual((status, body.splitlines()[0]), (200, "\ufeffcode_insee;url_page"))   # BOM : accents corrects dans Excel
        for bad in ("/rapport/80", "/rapport/..%2f..", "/csv/../config", "/inconnu"):
            self.assertEqual(self.call(bad)[0], 404, bad)


class PasswordTests(WebTests):
    password = "secret-équipe"

    def test_mot_de_passe_obligatoire(self):
        self.assertEqual(self.call("/")[0], 401)
        self.assertEqual(self.call("/", auth="faux")[0], 401)
        self.assertEqual(self.call("/", auth=self.password)[0], 200)
        self.assertEqual(self.call("/lancer", {"dept": "60"})[0], 401)
        self.assertIsNone(self.runner.proc)


class ServeTests(unittest.TestCase):
    def test_reseau_sans_mot_de_passe_refuse(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            webapp.serve(Path(tmp), "0.0.0.0", 0, "")

    def test_saisie_des_departements(self):
        self.assertEqual(webapp.parse_depts("60, 1 2a"), ["60", "01", "2A"])
        self.assertEqual(webapp.parse_depts("Tous"), ["all"])
        self.assertIsNone(webapp.parse_depts("60 ../x"))


if __name__ == "__main__":
    unittest.main()
