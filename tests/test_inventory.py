"""Tests du lot 1. Exécution : py -3 -m unittest discover -s tests -v"""
import csv
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import inventory as inv  # noqa: E402


def urls(value):
    return [c.url_candidate for c in inv.extract_url_candidates(value) if c.statut_url == inv.URL_NON_VERIFIEE]


class ExtractionTests(unittest.TestCase):
    def test_url_simple(self):
        self.assertEqual(urls("http://www.auxmarais.fr"), ["http://www.auxmarais.fr"])

    def test_liste_json_objets_valeur(self):  # structure réelle : liste Python déjà parsée
        self.assertEqual(urls([{"libelle": "", "valeur": "https://www.wavignies.fr/"}]),
                         ["https://www.wavignies.fr/"])

    def test_chaine_contenant_tableau_json(self):  # cas du CSV : le JSON est une chaîne
        raw = '[{"libelle": "", "valeur": "https://www.wavignies.fr/"}]'
        self.assertEqual(urls(raw), ["https://www.wavignies.fr/"])

    def test_dictionnaire_json(self):
        self.assertEqual(urls('{"valeur": "https://b.fr"}'), ["https://b.fr"])

    def test_valeurs_vides(self):
        for empty in ("", "   ", None, [], "[]", [{"libelle": "", "valeur": ""}]):
            self.assertEqual(inv.extract_url_candidates(empty), [], msg=repr(empty))

    def test_url_mal_formee_signalee_et_non_fatale(self):
        res = inv.extract_url_candidates("www.sans-schema.fr")
        self.assertEqual([r.statut_url for r in res], [inv.URL_INVALIDE])
        self.assertEqual(res[0].url_origine, "www.sans-schema.fr")  # origine conservée
        res = inv.extract_url_candidates('[{"valeur": "https://c.fr"')  # JSON tronqué
        self.assertEqual([r.statut_url for r in res], [inv.URL_INVALIDE])

    def test_libelle_non_pris_pour_une_url(self):
        raw = '[{"libelle": "Le site officiel d\'information", "valeur": "https://a.fr"}]'
        self.assertEqual(urls(raw), ["https://a.fr"])

    def test_plusieurs_urls_et_doublons_exacts(self):
        raw = ('[{"valeur": "https://e.fr"}, {"valeur": " https://e.fr "}, '
               '{"valeur": "http://f.fr"}, {"valeur": "https://e.fr/"}]')
        # doublon exact (après strip) retiré ; la variante avec "/" final est conservée
        self.assertEqual(urls(raw), ["https://e.fr", "http://f.fr", "https://e.fr/"])

    def test_http_et_https(self):
        self.assertEqual(urls('[{"valeur":"http://a.fr"},{"valeur":"https://b.fr"}]'),
                         ["http://a.fr", "https://b.fr"])

    def test_enregistrement_sans_url(self):
        self.assertEqual(inv.extract_site_urls({"nom": "x"}), [])
        self.assertEqual(inv.extract_site_urls({"site_internet": ""}), [])


class RealSampleTests(unittest.TestCase):
    """Échantillon d'enregistrements réels de dila_oise_all_records.csv."""

    @classmethod
    def setUpClass(cls):
        with (HERE / "fixtures" / "dila_sample.csv").open(encoding="utf-8-sig", newline="") as f:
            cls.records = list(csv.DictReader(f))
        cls.communes = [{"COM": c, "LIBELLE": n, "DEP": "60", "TYPECOM": "COM"} for c, n in [
            ("60701", "Wavignies"), ("60702", "Welles-Pérennes"), ("60703", "Aux Marais"),
            ("60245", "Formerie"), ("60611", "Senantes"), ("60999", "Commune sans enregistrement")]]
        cls.rows, cls.url_rows, cls.orphans = inv.build_inventory(cls.communes, cls.records, "T")
        cls.by = {r["code_insee"]: r for r in cls.rows}

    def test_type_service(self):
        t = {r["nom"]: inv.service_type(r) for r in self.records}
        self.assertEqual(t["Mairie - Wavignies"], "mairie")
        self.assertEqual(t["Mairie déléguée - Boutavent"], "mairie_deleguee")
        self.assertEqual(t["Centre communal d'action sociale (CCAS) - Wavignies"], "ccas")
        self.assertEqual(t["Groupement de gendarmerie départementale - Oise"], "gendarmerie_departementale")

    def test_wavignies_site_trouve(self):
        self.assertEqual(self.by["60701"]["statut_commune"], inv.SITE_TROUVE)
        self.assertEqual(self.by["60701"]["urls_candidates"], "https://www.wavignies.fr/")

    def test_welles_perennes_sans_url(self):
        self.assertEqual(self.by["60702"]["statut_commune"], inv.SANS_URL)

    def test_aux_marais_est_60703(self):
        self.assertEqual(self.by["60703"]["urls_candidates"], "http://www.auxmarais.fr")

    def test_autres_services_non_attribues(self):
        joined = " ".join(r["urls_candidates"] for r in self.rows)
        self.assertNotIn("gendarmerie.interieur.gouv.fr", joined)
        self.assertNotIn("france-services.gouv.fr", joined)

    def test_plusieurs_candidats(self):
        self.assertEqual(self.by["60611"]["statut_commune"], inv.PLUSIEURS_SITES)
        self.assertEqual(self.by["60611"]["nb_urls_valides"], 2)

    def test_commune_sans_enregistrement(self):
        self.assertEqual(self.by["60999"]["statut_commune"], inv.AUCUN_ENREGISTREMENT)

    def test_mairie_deleguee_rattachee_a_la_commune_nouvelle(self):
        # pivot : ["60096" (déléguée, absent du COG), "60245" (Formerie)]
        self.assertEqual(self.by["60245"]["nb_enregistrements_mairie"], 2)
        self.assertEqual(self.orphans, [])


class RetryTests(unittest.TestCase):
    def _http_error(self, code):
        from urllib.error import HTTPError
        return HTTPError("http://x", code, "err", {}, None)

    def test_retry_sur_500_puis_succes(self):
        from unittest import mock
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"ok"
        calls = [self._http_error(500), Resp()]
        with mock.patch.object(inv, "urlopen", side_effect=calls) as m:
            self.assertEqual(inv.fetch_bytes("http://x", backoff=0), b"ok")
            self.assertEqual(m.call_count, 2)

    def test_pas_de_retry_sur_404_et_echec_apres_3_500(self):
        from unittest import mock
        from urllib.error import HTTPError
        with mock.patch.object(inv, "urlopen", side_effect=self._http_error(404)) as m:
            with self.assertRaises(HTTPError):
                inv.fetch_bytes("http://x", backoff=0)
            self.assertEqual(m.call_count, 1)
        with mock.patch.object(inv, "urlopen", side_effect=self._http_error(500)) as m:
            with self.assertRaises(HTTPError):
                inv.fetch_bytes("http://x", backoff=0)
            self.assertEqual(m.call_count, 3)


if __name__ == "__main__":
    unittest.main()
