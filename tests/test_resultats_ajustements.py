"""Ajustements de l'onglet Résultats après contrôle d'accessibilité : tri sans accents, menu, états annoncés."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import publications  # noqa: E402
import report  # noqa: E402


def ligne(nom: str, code: str = "60001") -> dict:
    return {"dept": "60", "code_insee": code, "nom_commune": nom, "url_page": "https://x.fr/"}


class TriSansAccentsTests(unittest.TestCase):
    def test_commune_accentuee_rangee_avec_sa_lettre(self):
        noms = [l["nom_commune"] for l in publications.trier_liens(
            [ligne("Zeta"), ligne("Élancourt"), ligne("Dax"), ligne("Eu"), ligne("Étaples")])]
        self.assertEqual(noms, ["Dax", "Élancourt", "Eu", "Étaples", "Zeta"])

    def test_tri_stable_a_nom_equivalent(self):
        a, b = ligne("Été", "02"), ligne("Ete", "01")
        self.assertEqual([r["code_insee"] for r in publications.trier_liens([a, b])], ["01", "02"])


class MenuTests(unittest.TestCase):
    def test_bouton_menu_sans_aria_haspopup(self):
        html = report.page("T", "<p>x</p>", active="analyses")
        self.assertIn('id="button-open-menu"', html)
        self.assertNotIn("aria-haspopup", html)


if __name__ == "__main__":
    unittest.main()
