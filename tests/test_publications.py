"""Lecture des publications depuis les fichiers (FileSource) : sans Postgres."""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402,F401  (chemins src/ et tests/)
from publications import CSV_TOUS, FileSource, Publication  # noqa: E402


class FileSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sp.construire_racine(self.root)
        self.src = FileSource(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_depts_tries_et_filtres(self):
        (self.root / "reports" / "2A").mkdir()
        (self.root / "reports" / "971").mkdir()
        (self.root / "reports" / "1").mkdir()            # un seul chiffre : code invalide
        (self.root / "reports" / "ABC").mkdir()          # ni code ni département
        (self.root / "reports" / "fichier.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.src.depts(), ["02", "2A", "60", "80", "971"])

    def test_depts_sans_dossier_reports(self):
        with tempfile.TemporaryDirectory() as vide:
            self.assertEqual(FileSource(Path(vide)).depts(), [])
            self.assertIsNone(FileSource(Path(vide)).get("60"))
            self.assertIsNone(FileSource(Path(vide)).csv_all())

    def test_get_champs_complets(self):
        pub = self.src.get("60")
        self.assertIsInstance(pub, Publication)
        self.assertEqual(pub.dept, "60")
        self.assertTrue(pub.controle_date)                      # ISO du dernier contrôle
        self.assertEqual((pub.communes, pub.avec_lien), (1, 1))
        self.assertIn("Compte rendu 60", pub.report_html)
        self.assertTrue(pub.csv_present)
        self.assertEqual(pub.csv_bytes, (self.root / "resultats" / "liens_60.csv").read_bytes())
        self.assertIn("premier contrôle", pub.resume_changements)

    def test_csv_servi_en_octets_avec_bom(self):
        pub = self.src.get("60")
        self.assertTrue(pub.csv_bytes.startswith(sp.BOM))
        self.assertIsInstance(pub.csv_bytes, bytes)
        self.assertEqual(self.src.csv_all(), sp.CSV_TOUS_OCTETS)  # octets tels quels, sans réencodage

    def test_report_et_csv_facultatifs_laisses_vides(self):
        complet = self.src.get("60")
        leger = self.src.get("60", report=False, csv=False)
        self.assertEqual(leger.report_html, "")
        self.assertEqual(leger.csv_bytes, b"")
        self.assertTrue(leger.csv_present)                      # l'existence du fichier reste connue
        self.assertEqual(
            (leger.controle_date, leger.communes, leger.avec_lien, leger.resume_changements),
            (complet.controle_date, complet.communes, complet.avec_lien, complet.resume_changements))

    def test_csv_seul_ou_rapport_seul(self):
        sans_rapport = self.src.get("60", report=False)
        self.assertEqual(sans_rapport.report_html, "")
        self.assertTrue(sans_rapport.csv_bytes.startswith(sp.BOM))
        sans_csv = self.src.get("60", csv=False)
        self.assertIn("Compte rendu 60", sans_csv.report_html)
        self.assertEqual(sans_csv.csv_bytes, b"")

    def test_departement_sans_csv(self):
        pub = self.src.get("80")
        self.assertFalse(pub.csv_present)
        self.assertEqual(pub.csv_bytes, b"")
        self.assertEqual(pub.avec_lien, 0)
        self.assertIn("Compte rendu 80", pub.report_html)

    def test_departement_sans_controle(self):
        pub = self.src.get("02")
        self.assertEqual((pub.controle_date, pub.communes, pub.avec_lien, pub.resume_changements),
                         ("", 0, 0, ""))
        self.assertFalse(pub.csv_present)
        self.assertIn("Compte rendu 02", pub.report_html)

    def test_departement_inconnu_none(self):
        for code in ("99", "2B", "7", "60 ", "../60"):
            with self.subTest(code=code):
                self.assertIsNone(self.src.get(code))

    def test_csv_tous_present_et_absent(self):
        self.assertEqual(CSV_TOUS, "liens_tous_departements.csv")
        self.assertEqual(self.src.csv_all(), sp.CSV_TOUS_OCTETS)
        (self.root / "resultats" / CSV_TOUS).unlink()
        self.assertIsNone(self.src.csv_all())

    def test_publication_immuable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.src.get("60").dept = "61"


if __name__ == "__main__":
    unittest.main()
