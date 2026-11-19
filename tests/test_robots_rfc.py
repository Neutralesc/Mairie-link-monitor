"""Analyseur robots.txt (RFC 9309) : groupes, motifs les plus longs, jokers, Crawl-delay, Sitemap, décodage."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from robots_rfc import RobotsRules  # noqa: E402

BOT = "MairieLinkMonitor/0.11 (detection de liens) contact"
M = "https://m.fr"


def rules(text: str) -> RobotsRules:
    rp = RobotsRules()
    rp.parse(text.splitlines())
    return rp


class RobotsRfcTests(unittest.TestCase):
    def test_motif_le_plus_long_l_emporte(self):
        rp = rules("User-agent: *\nDisallow: /prive\nAllow: /prive/public\n")
        self.assertFalse(rp.can_fetch(BOT, M + "/prive/x"))
        self.assertTrue(rp.can_fetch(BOT, M + "/prive/public/doc"))
        self.assertTrue(rp.can_fetch(BOT, M + "/prive/public"))

    def test_allow_gagne_a_egalite_de_longueur(self):
        rp = rules("User-agent: *\nDisallow: /a\nAllow: /a\n")
        self.assertTrue(rp.can_fetch(BOT, M + "/a/b"))
        rp = rules("User-agent: *\nAllow: /a\nDisallow: /a\n")          # même résultat quel que soit l'ordre
        self.assertTrue(rp.can_fetch(BOT, M + "/a/b"))

    def test_joker_et_ancre_fin(self):
        rp = rules("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(rp.can_fetch(BOT, M + "/doc.pdf"))
        self.assertTrue(rp.can_fetch(BOT, M + "/doc.pdf?x=1"))          # l'ancre $ exclut la suite de l'URL
        self.assertTrue(rp.can_fetch(BOT, M + "/doc.pdfx"))
        self.assertFalse(rp.can_fetch(BOT, M + "/dossier/rapport.pdf"))  # le joker couvre plusieurs segments

    def test_groupe_specifique_prioritaire_sur_joker(self):
        text = "User-agent: *\nDisallow: /\n\nUser-agent: MairieLinkMonitor\nAllow: /\n"
        rp = rules(text)
        self.assertTrue(rp.can_fetch(BOT, M + "/page"))
        self.assertFalse(rp.can_fetch("AutreRobot/1.0", M + "/page"))   # pas de groupe spécifique : groupe « * »

    def test_user_agent_multiples_partagent_le_groupe(self):
        rp = rules("User-agent: Autre\nUser-agent: MairieLinkMonitor\nDisallow: /secret\n")
        self.assertFalse(rp.can_fetch(BOT, M + "/secret"))
        self.assertFalse(rp.can_fetch("Autre/1.0", M + "/secret"))       # les deux agents partagent le même groupe

    def test_disallow_vide_n_interdit_rien(self):
        rp = rules("User-agent: *\nDisallow:\n")
        self.assertTrue(rp.can_fetch(BOT, M + "/anything/at/all"))

    def test_crawl_delay_decimal(self):
        rp = rules("User-agent: *\nCrawl-delay: 2.5\n")
        self.assertEqual(rp.crawl_delay(BOT), 2.5)

    def test_crawl_delay_invalide_ou_negatif_donne_none(self):
        for value in ("abc", "-1", "inf", "nan"):
            rp = rules(f"User-agent: *\nCrawl-delay: {value}\n")
            self.assertIsNone(rp.crawl_delay(BOT), value)

    def test_commentaires_et_bom(self):
        rp = rules("﻿User-agent: * # tous\nDisallow: /secret # note\n# ligne entière\n\nAllow: /secret/ok\n")
        self.assertFalse(rp.can_fetch(BOT, M + "/secret/x"))
        self.assertTrue(rp.can_fetch(BOT, M + "/secret/ok"))

    def test_sitemap_declares(self):
        rp = rules("User-agent: *\nDisallow: /x\nSitemap: https://m.fr/s1.xml\nSitemap: https://m.fr/s2.xml\n")
        self.assertEqual(rp.site_maps(), ["https://m.fr/s1.xml", "https://m.fr/s2.xml"])
        self.assertIsNone(rules("User-agent: *\nDisallow: /x\n").site_maps())

    def test_robots_vide_tout_est_autorise(self):
        rp = rules("")
        self.assertTrue(rp.can_fetch(BOT, M + "/quoi"))
        self.assertIsNone(rp.crawl_delay(BOT))
        self.assertIsNone(rp.site_maps())

    def test_robots_txt_toujours_autorise(self):
        rp = rules("User-agent: *\nDisallow: /\n")
        self.assertTrue(rp.can_fetch(BOT, M + "/robots.txt"))
        self.assertFalse(rp.can_fetch(BOT, M + "/"))

    def test_correspondance_apres_decodage_des_pourcentages(self):
        rp = rules("User-agent: *\nDisallow: /caf%C3%A9\n")
        self.assertFalse(rp.can_fetch(BOT, M + "/caf%C3%A9/menu"))
        self.assertFalse(rp.can_fetch(BOT, M + "/café/menu"))
        self.assertTrue(rp.can_fetch(BOT, M + "/cafe/menu"))

    def test_directive_hors_groupe_ignoree(self):
        rp = rules("Disallow: /tout\nUser-agent: *\nDisallow: /secret\n")
        self.assertTrue(rp.can_fetch(BOT, M + "/tout"))
        self.assertFalse(rp.can_fetch(BOT, M + "/secret"))


if __name__ == "__main__":
    unittest.main()
