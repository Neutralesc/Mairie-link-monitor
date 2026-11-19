"""Commande ci_analyse (run / fin) en sous-processus avec un faux crawler. Sans réseau.

Les classes PostgreSQL sont ignorées sans MLM_TEST_DATABASE_URL ; la classe CiSansBase tourne toujours (aucun accès
à la base dans ces cas).
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402

import store_pg  # noqa: E402

CI = str(sp.RACINE / "src" / "ci_analyse.py")
URL_INEXISTANTE = "postgresql://inexistant.invalid/x"  # ne doit jamais être contactée par les cas testés
VARIABLES_ANALYSE = ("DATABASE_URL", "MLM_CONTACT", "GITHUB_RUN_ID", "FAUX_MODE")


def lancer_ci(args, **env):
    base = {k: v for k, v in os.environ.items() if k not in VARIABLES_ANALYSE}
    base.update(PYTHONIOENCODING="utf-8", **env)
    return subprocess.run([sys.executable, CI, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=base, timeout=120)


def attendre(condition, delai=20.0, pas=0.05) -> bool:
    """Sondage borné : True dès que `condition()` est vraie, False à l'échéance."""
    limite = time.monotonic() + delai
    while time.monotonic() < limite:
        if condition():
            return True
        time.sleep(pas)
    return bool(condition())


def processus_vivant(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CiSansBaseTests(unittest.TestCase):
    """Cas de validation : code 2 avant tout accès à la base."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_depts_invalides_code_2_sans_toucher_la_base(self):
        r = lancer_ci(["run", "--analysis-id", "1", "--depts", "60; rm -rf /", "--root", str(self.root)],
                      DATABASE_URL=URL_INEXISTANTE)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("invalide", r.stderr)
        self.assertNotIn("inexistant", r.stderr)              # aucune tentative de connexion
        self.assertFalse((self.root / "demarre.txt").exists())

    def test_database_url_absente_code_2(self):
        r = lancer_ci(["run", "--analysis-id", "1", "--depts", "tous", "--root", str(self.root)], DATABASE_URL="")
        self.assertEqual(r.returncode, 2)
        self.assertIn("DATABASE_URL", r.stderr)

    def test_fin_statut_non_autorise_code_2(self):
        r = lancer_ci(["fin", "--analysis-id", "1", "--statut", "terminee"], DATABASE_URL=URL_INEXISTANTE)
        self.assertEqual(r.returncode, 2)
        self.assertIn("terminee", r.stderr)

    def test_fin_sans_database_url_code_2(self):
        r = lancer_ci(["fin", "--analysis-id", "1", "--statut", "annulee"], DATABASE_URL="   ")
        self.assertEqual(r.returncode, 2)
        self.assertIn("DATABASE_URL", r.stderr)


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class CiPgTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "mairie_link_monitor.py").write_text(sp.FAUX_CRAWLER, encoding="utf-8")
        self.sortie = Path(self.tmp.name) / "sortie.txt"
        self.processus: list[int] = []

    def tearDown(self):
        for pid in self.processus:                             # filet de sécurité : rien ne doit survivre au test
            if processus_vivant(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.tmp.cleanup()

    # ---- aides
    def creer(self, statut_final: str | None, depts: list[str], reprise: bool = False) -> int:
        conn = store_pg.connect(self.url)
        try:
            aid = store_pg.analysis_queue(conn, depts, reprise)
            if statut_final is not None:
                store_pg.analysis_end(conn, aid, statut_final)
            return aid
        finally:
            conn.close()

    def lire(self, aid: int) -> dict:
        conn = store_pg.connect(self.url)
        try:
            return store_pg.analysis_get(conn, aid)
        finally:
            conn.close()

    def lancer_analyse(self, aid: int, depts: str = "60 80", mode: str = "ok", extra=(), **env):
        args = ["run", "--analysis-id", str(aid), "--depts", depts, "--root", str(self.root), *extra]
        return lancer_ci(args, DATABASE_URL=self.url, FAUX_MODE=mode, **env)

    def publication(self):
        pg = store_pg.PostgresSource(self.url)
        return pg.depts(), pg.csv_all()

    # ---- run
    def test_run_ok_terminee_journal_et_publication(self):
        aid = self.creer(None, ["60"])
        r = self.lancer_analyse(aid, mode="ok", GITHUB_RUN_ID="run-ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.lire(aid)
        self.assertEqual((row["statut"], row["run_id"]), ("terminee", "run-ok"))
        self.assertIsNotNone(row["fin"])
        self.assertIn("faux crawler", row["journal"])
        self.assertIn("code retour du crawler : 0 ; statut : terminee", row["journal"])
        self.assertIn("publié : 60 ; CSV global : oui", r.stdout)
        depts, csv = self.publication()
        self.assertEqual(depts, ["60"])
        self.assertIsNotNone(csv)

    def test_run_transmet_le_contact_facultatif_et_les_depts(self):
        aid = self.creer(None, ["60"])
        r = self.lancer_analyse(aid, depts="60 80", MLM_CONTACT="contact-de-test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dept 60 80", r.stdout)
        self.assertIn("--contact=contact-de-test", self.lire(aid)["journal"])

    def test_run_echec_code_1_publication_tout_de_meme(self):
        aid = self.creer(None, ["60"], reprise=True)
        r = self.lancer_analyse(aid, mode="echec", GITHUB_RUN_ID="run-fail", extra=["--reprise"])
        self.assertEqual(r.returncode, 1, r.stderr)
        row = self.lire(aid)
        self.assertEqual(row["statut"], "echec")
        self.assertIn("erreur simulee", row["journal"])
        self.assertIn("--resume", row["journal"])
        self.assertEqual(self.publication()[0], ["60"])        # résultat partiel publié malgré l'échec

    def test_run_sigterm_annulee_code_130_sans_processus_restant(self):
        aid = self.creer(None, ["60"])
        env = {k: v for k, v in os.environ.items() if k not in VARIABLES_ANALYSE}
        env.update(PYTHONIOENCODING="utf-8", DATABASE_URL=self.url, FAUX_MODE="attente", GITHUB_RUN_ID="run-term")
        args = [sys.executable, CI, "run", "--analysis-id", str(aid), "--depts", "60", "--root", str(self.root)]
        with open(self.sortie, "w", encoding="utf-8") as sortie:
            p = subprocess.Popen(args, stdout=sortie, stderr=subprocess.STDOUT, env=env)
        try:
            pret = self.root / "pret.txt"

            def pid_crawler():
                try:
                    return int(pret.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None

            self.assertTrue(attendre(lambda: pid_crawler() is not None), "crawler jamais démarré")
            pid = pid_crawler()
            self.processus.append(pid)
            p.send_signal(signal.SIGTERM)
            code = p.wait(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=10)
        self.assertEqual(code, 130)
        row = self.lire(aid)
        self.assertEqual(row["statut"], "annulee")
        self.assertIsNotNone(row["fin"])
        self.assertIn("signal SIGTERM reçu : arrêt du crawler", row["journal"])
        self.assertIn("faux crawler", row["journal"])
        self.assertEqual(self.publication()[0], ["60"])        # publication faite même après l'arrêt
        self.assertFalse(processus_vivant(pid), "le faux crawler tourne encore après l'arrêt")

    def test_run_analyse_annulee_avant_demarrage_code_0_sans_lancement(self):
        aid = self.creer("annulee", ["60"])
        r = self.lancer_analyse(aid, mode="ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rien à lancer", r.stdout)
        self.assertFalse((self.root / "demarre.txt").exists())   # le crawler n'a jamais été lancé
        self.assertEqual(self.lire(aid)["statut"], "annulee")
        self.assertEqual(self.publication(), ([], None))

    def test_run_analyse_inconnue_code_0_sans_lancement(self):
        r = self.lancer_analyse(987654)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rien à lancer", r.stdout)
        self.assertFalse((self.root / "demarre.txt").exists())

    # ---- fin
    def test_fin_annulee_sur_analyse_en_file(self):
        aid = self.creer(None, ["60"])
        r = lancer_ci(["fin", "--analysis-id", str(aid), "--statut", "annulee"], DATABASE_URL=self.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("statut « annulee »", r.stdout)
        self.assertEqual(self.lire(aid)["statut"], "annulee")

    def test_fin_sans_effet_sur_statut_final(self):
        aid = self.creer("terminee", ["60"])
        fin = self.lire(aid)["fin"]
        r = lancer_ci(["fin", "--analysis-id", str(aid), "--statut", "echec"], DATABASE_URL=self.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("déjà finale", r.stdout)
        row = self.lire(aid)
        self.assertEqual((row["statut"], row["fin"]), ("terminee", fin))


if __name__ == "__main__":
    unittest.main()
