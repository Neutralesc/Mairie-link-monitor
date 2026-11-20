"""Application web : pages, lancement et arrêt d'une analyse, mot de passe, protection des formulaires."""
import base64
import gzip
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
import report  # noqa: E402
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

    def location(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            self.opener.open(req, timeout=5)
        except urllib.error.HTTPError as e:
            return e.headers.get("Location")
        return None

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
        corps = html[html.index("<main"):html.index("</main>")]
        self.assertNotIn("Lancer une analyse", html)
        self.assertIn('lang="fr"', html)
        self.assertIn("départements publiés", html)                              # chiffres de l'accueil
        self.assertIn("Aucune analyse pour le moment.", html)
        self.assertNotIn('href="/analyse"', corps)                               # le lancement n'est pas dans le corps
        self.assertIn('href="/analyse"', html)                                   # ... mais reste dans le menu admin
        self.assertIn('name="departement"', self.call("/analyse")[1])            # le choix des départements est sur /analyse

    def test_lancer_suivre_arreter(self):
        status, _ = self.call("/lancer", {"dept": "60 1", "reprise": "1"})
        self.assertEqual(status, 303)
        self.wait_log("ARGS --dept 60 01 --resume")
        accueil = self.call("/")[1]
        self.assertIn("Une analyse est en cours", accueil)                      # l'accueil signale l'analyse...
        self.assertNotIn('href="/suivi"', accueil[accueil.index("<main"):accueil.index("</main>")])  # ... sans lien
        status, html = self.call("/suivi")
        self.assertIn("Analyse en cours : 60 01 (reprise)", html)
        self.assertNotIn('http-equiv="refresh"', html)                          # RGAA 13.1 : pas de rechargement imposé
        self.assertNotIn("actualiser-auto", html)                               # ... ni de rechargement automatique
        self.assertIn('href="/suivi"', html)                                    # actualisation manuelle : lien « Actualiser »
        self.assertIn("Actualiser", html)
        self.assertEqual(self.call("/lancer", {"dept": "80"})[0], 409)          # une seule analyse à la fois
        self.assertEqual(self.call("/arreter", {})[0], 303)
        self.runner.proc.wait(5)
        self.assertNotIn("Analyse en cours", self.call("/suivi")[1])
        self.assertNotIn("Une analyse est en cours", self.call("/")[1])

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
        liste = self.call("/rapports")[1]
        self.assertIn('href="/rapports/60"', liste)                             # page « Rapports » : un rapport par département
        self.assertIn('href="/rapports/60/communes.csv"', liste)
        status, html = self.call("/rapports/60")
        self.assertEqual(status, 200)
        self.assertIn("Communes avec un lien (1)", html)
        # Ancienne adresse : redirigée vers la page du département publié, 404 pour un département non publié.
        self.assertEqual(self.call("/rapport/60")[0], 303)
        self.assertEqual(self.location("/rapport/60"), "/rapports/60")
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


# ----------------------------------------------------------------- correctifs de fiabilité (lot B)

GROS_CSV = ("code_insee;url_page\r\n"
            + "".join(f"{i:05d};https://site-{i}.fr/page-{i}\r\n" for i in range(3000))).encode("utf-8")
PETIT_CSV = "code_insee;url_page\r\n60001;https://a.fr/é\r\n".encode("utf-8")


class BrutWebTests(WebTests):
    """Requêtes qui renvoient les octets bruts et les en-têtes (WebTests.call décode le texte)."""

    def brut(self, path, en_tetes=None, data=None):
        req = urllib.request.Request(self.base + path, data=data)
        for nom, valeur in (en_tetes or {}).items():
            req.add_header(nom, valeur)
        try:
            with self.opener.open(req, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def verifier_en_tetes_securite(self, en_tetes):
        self.assertEqual(en_tetes.get("X-Frame-Options"), "DENY")
        self.assertEqual(en_tetes.get("Referrer-Policy"), "same-origin")
        politique = en_tetes.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'none'", politique)
        self.assertIn("default-src 'self'", politique)


class CsvTousCompressionTests(BrutWebTests):
    def ecrire_csv_global(self, octets: bytes) -> None:
        (self.root / "resultats").mkdir(exist_ok=True)
        (self.root / "resultats" / "liens_tous_departements.csv").write_bytes(octets)

    def test_gros_csv_compresse_si_gzip_accepte(self):
        self.assertGreater(len(GROS_CSV), 64 * 1024)
        self.ecrire_csv_global(GROS_CSV)
        status, en_tetes, corps = self.brut("/csv/tous", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(en_tetes.get("Content-Encoding"), "gzip")
        self.assertEqual(en_tetes.get("Vary"), "Accept-Encoding")
        self.assertEqual(gzip.decompress(corps), GROS_CSV)      # octets identiques à la source
        self.assertLess(len(corps), len(GROS_CSV))

    def test_gros_csv_compresse_si_x_gzip_accepte(self):
        self.ecrire_csv_global(GROS_CSV)
        status, en_tetes, corps = self.brut("/csv/tous", {"Accept-Encoding": "x-gzip"})
        self.assertEqual(en_tetes.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(corps), GROS_CSV)

    def test_gros_csv_sans_en_tete_octets_identiques(self):
        self.ecrire_csv_global(GROS_CSV)
        status, en_tetes, corps = self.brut("/csv/tous")
        self.assertEqual(status, 200)
        self.assertIsNone(en_tetes.get("Content-Encoding"))
        self.assertEqual(corps, GROS_CSV)

    def test_gros_csv_gzip_q0_refuse(self):
        self.ecrire_csv_global(GROS_CSV)
        status, en_tetes, corps = self.brut("/csv/tous", {"Accept-Encoding": "gzip;q=0"})
        self.assertIsNone(en_tetes.get("Content-Encoding"))
        self.assertEqual(corps, GROS_CSV)

    def test_petit_csv_jamais_compresse(self):
        self.ecrire_csv_global(PETIT_CSV)
        status, en_tetes, corps = self.brut("/csv/tous", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertIsNone(en_tetes.get("Content-Encoding"))
        self.assertEqual(corps, PETIT_CSV)


class LancerLongueurInvalideTests(BrutWebTests):
    def poster(self, longueur: str):
        # Corps vide : le serveur refuse avant toute lecture du corps, donc rien à désynchroniser.
        return self.brut("/lancer", {"Origin": self.base, "Content-Length": longueur}, data=b"")

    def test_content_length_non_numerique_400_sans_analyse(self):
        self.assertEqual(self.poster("abc")[0], 400)
        self.assertIsNone(self.runner.proc)

    def test_content_length_negatif_400_sans_analyse(self):
        self.assertEqual(self.poster("-5")[0], 400)
        self.assertIsNone(self.runner.proc)

    def test_content_length_trop_grand_400_sans_analyse(self):
        self.assertEqual(self.poster("99999")[0], 400)
        self.assertIsNone(self.runner.proc)


class EnTetesSecuriteTests(BrutWebTests):
    def test_accueil_en_tetes_de_securite(self):
        status, en_tetes, _ = self.brut("/")
        self.assertEqual(status, 200)
        self.verifier_en_tetes_securite(en_tetes)


class EnTetesSecuriteProtegesTests(BrutWebTests):
    password = "secret-entetes-7f"

    def test_401_en_tetes_de_securite(self):
        status, en_tetes, _ = self.brut("/")
        self.assertEqual(status, 401)
        self.verifier_en_tetes_securite(en_tetes)


class ReportLinkTests(unittest.TestCase):
    def test_schema_javascript_sans_lien_et_texte_echappe(self):
        piege = 'javascript:alert("1")<img src=x onerror=alert(2)>'
        html = report.link(piege)
        self.assertNotIn("<a", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertIn("javascript:alert(&quot;1&quot;)", html)

    def test_https_avec_lien(self):
        html = report.link("https://x.fr/a")
        self.assertIn('<a href="https://x.fr/a"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_schema_http_majuscules_avec_lien(self):
        self.assertIn('<a href="HTTP://x.fr"', report.link("HTTP://x.fr"))


if __name__ == "__main__":
    unittest.main()
