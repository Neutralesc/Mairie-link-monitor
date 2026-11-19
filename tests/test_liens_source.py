"""Liste structurée des communes avec lien : FileSource.liens, publications.trier_liens, store_pg.publish et
PostgresSource.liens (base de test seulement si MLM_TEST_DATABASE_URL est définie). Aucun réseau externe.
"""
import random
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402  (chemins src/ et tests/)
import csvio  # noqa: E402
import store_pg  # noqa: E402
from layout import DeptPaths  # noqa: E402
from publications import FileSource, trier_liens  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

CLES = {"dept", "code_insee", "nom_commune", "url_page"}
LIEN_60 = {"dept": "60", "code_insee": "60001", "nom_commune": "A", "url_page": "https://a.fr/"}


def ecrire_dept(root: Path, dept: str, communes, liens) -> None:
    """communes : [(code, nom)] écrit l'inventaire (None = pas d'inventaire) ; liens : [(code, url)] écrit
    links_found.csv (None = pas d'exploration). Les listes fournies ne doivent pas être vides."""
    p = DeptPaths(root, dept)
    p.reports_dir.mkdir(parents=True, exist_ok=True)
    if communes is not None:
        csvio.write_rows(p.inventory_communes, [{"code_insee": c, "nom_commune": n, "nb_urls_valides": "1"}
                                                for c, n in communes])
    if liens is not None:
        csvio.write_rows(p.links_found, [{"code_insee": c, "site_url": u, "page_url": u, "type_lien": "lien"}
                                         for c, u in liens])


def racine_liens(root: Path) -> None:
    """01 : deux communes avec lien ; 60 : deux communes dont une avec lien ; 80 : inventaire sans exploration."""
    ecrire_dept(root, "01", [("01001", "L'Abergement-Clemenciat"), ("01002", "Ambérieu-en-Bugey")],
                [("01001", "https://abergement.example/"), ("01002", "https://amberieu.example/")])
    ecrire_dept(root, "60", [("60001", "A"), ("60002", "Châtillon")],
                [("60001", "https://a.fr/"), ("60002", "https://chatillon.example/")])
    ecrire_dept(root, "80", [("80001", "Amiens")], None)


def attendu_racine_liens() -> list[dict]:
    """Résultat attendu de racine_liens : 4 lignes, triées par dept puis nom (casse ignorée) puis code."""
    return [
        {"dept": "01", "code_insee": "01002", "nom_commune": "Ambérieu-en-Bugey", "url_page": "https://amberieu.example/"},
        {"dept": "01", "code_insee": "01001", "nom_commune": "L'Abergement-Clemenciat",
         "url_page": "https://abergement.example/"},
        {"dept": "60", "code_insee": "60001", "nom_commune": "A", "url_page": "https://a.fr/"},
        {"dept": "60", "code_insee": "60002", "nom_commune": "Châtillon", "url_page": "https://chatillon.example/"},
    ]


class FileSourceLiensTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        sp.construire_racine(self.root)
        self.src = FileSource(self.root)

    def racine_neuve(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_fixture_une_ligne_aux_bons_champs(self):
        liens = self.src.liens()
        self.assertEqual(liens, [LIEN_60])
        self.assertEqual(set(liens[0]), CLES)

    def test_filtre_par_departement(self):
        self.assertEqual(self.src.liens("60"), [LIEN_60])
        self.assertEqual(self.src.liens("80"), [])

    def test_departement_inconnu_renvoie_une_liste_vide(self):
        for dept in ("99", "2A", "ZZ", ""):
            with self.subTest(dept=dept):
                self.assertEqual(self.src.liens(dept), [])

    def test_departement_sans_exploration_ignore_sans_exception(self):
        # 80 : inventaire seulement (pas de links_found) ; 02 : dossier vide. Ni ligne ni exception.
        self.assertEqual(self.src.depts(), ["02", "60", "80"])
        self.assertEqual(self.src.liens("02"), [])
        self.assertEqual(self.src.liens("80"), [])
        self.assertEqual(self.src.liens(), [LIEN_60])

    def test_racine_vide(self):
        vide = FileSource(self.racine_neuve())
        self.assertEqual(vide.liens(), [])
        self.assertEqual(vide.liens("60"), [])

    def test_nom_vide_si_commune_absente_de_l_inventaire(self):
        racine = self.racine_neuve()
        ecrire_dept(racine, "2A", None, [("2A004", "https://ajaccio.example/")])
        self.assertEqual(FileSource(racine).liens("2A"),
                         [{"dept": "2A", "code_insee": "2A004", "nom_commune": "", "url_page": "https://ajaccio.example/"}])

    def test_tri_departement_puis_nom_insensible_a_la_casse_puis_code(self):
        racine = self.racine_neuve()
        ecrire_dept(racine, "60", [("60003", "beaumont"), ("60002", "Alpha"), ("60001", "alpha"), ("60004", "Zeta")],
                    [(c, f"https://{c}.example/") for c in ("60003", "60002", "60001", "60004")])
        ecrire_dept(racine, "01", [("01001", "Zoo")], [("01001", "https://zoo.example/")])
        ordre = [(r["dept"], r["code_insee"]) for r in FileSource(racine).liens()]
        self.assertEqual(ordre, [("01", "01001"), ("60", "60001"), ("60", "60002"), ("60", "60003"), ("60", "60004")])

    def test_nom_avec_accents_apostrophe_et_guillemets_relu_intact(self):
        racine = self.racine_neuve()
        nom = 'L\'Été "du Nord" – Château'
        ecrire_dept(racine, "59", [("59001", nom)], [("59001", "https://nord.example/")])
        self.assertEqual(FileSource(racine).liens("59")[0]["nom_commune"], nom)


class TrierLiensTests(unittest.TestCase):
    def rangees(self):
        return [
            {"dept": "60", "code_insee": "60010", "nom_commune": "Élancourt", "url_page": "u1"},
            {"dept": "60", "code_insee": "60011", "nom_commune": "élan", "url_page": "u2"},
            {"dept": "60", "code_insee": "60012", "nom_commune": "Élan", "url_page": "u3"},
            {"dept": "60", "code_insee": "60013", "nom_commune": "Zeta", "url_page": "u4"},
            {"dept": "02", "code_insee": "02001", "nom_commune": "Été", "url_page": "u5"},
        ]

    def test_resultat_stable_quel_que_soit_l_ordre_d_entree(self):
        rangees = self.rangees()
        attendu = trier_liens(rangees)
        melanges = [rangees[::-1], rangees[2:] + rangees[:2], random.Random(7).sample(rangees, len(rangees))]
        for ordre in melanges:
            with self.subTest(ordre=[r["code_insee"] for r in ordre]):
                self.assertEqual(trier_liens(ordre), attendu)

    def test_casse_ignoree_egalites_departagees_par_le_code(self):
        # « élan » et « Élan » ont le même casefold : l'ordre dépend alors du code INSEE, pas de la casse d'origine.
        ex_aequo = [r["code_insee"] for r in trier_liens(self.rangees()) if r["nom_commune"].casefold() == "élan"]
        self.assertEqual(ex_aequo, ["60011", "60012"])

    def test_departement_d_abord(self):
        self.assertEqual(trier_liens(self.rangees())[0]["dept"], "02")

    def test_entree_non_modifiee(self):
        rangees = self.rangees()
        copie = [dict(r) for r in rangees]
        trier_liens(rangees)
        self.assertEqual(rangees, copie)


@unittest.skipIf(psycopg is None, "psycopg non installé : test de délai ignoré")
class ConnectTimeoutLiensTests(unittest.TestCase):
    URL = "postgresql://utilisateur-test@hote-test/base-test"

    def test_postgres_source_liens_transmet_connect_timeout(self):
        source = store_pg.PostgresSource(self.URL)
        with mock.patch("psycopg.connect") as connecter:
            self.assertEqual(source.liens(), [])          # faux curseur vide : aucune base contactée
        self.assertEqual(connecter.call_args.args, (self.URL,))
        self.assertEqual(connecter.call_args.kwargs.get("connect_timeout"), 5)

    def test_postgres_source_liens_dept_transmet_connect_timeout(self):
        source = store_pg.PostgresSource(self.URL)
        with mock.patch("psycopg.connect") as connecter:
            self.assertEqual(source.liens("60"), [])
        self.assertEqual(connecter.call_args.kwargs.get("connect_timeout"), 5)


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class PublierLiensPgTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.racine = Path(tmp.name)
        racine_liens(self.racine)

    def publier(self, racine: Path, url: str | None = None, only=None) -> dict:
        conn = store_pg.connect(url or self.url)
        try:
            return store_pg.publish(FileSource(racine), conn, only)
        finally:
            conn.close()

    def executer(self, sql: str, params=()) -> list:
        """Requête directe dans le schéma de test ; renvoie les lignes si la requête en produit, sinon []."""
        with psycopg.connect(self.url, autocommit=True) as conn:
            curseur = conn.execute(sql, params)
            return curseur.fetchall() if curseur.description else []

    def test_publish_puis_liens_identiques_au_fichier(self):
        self.publier(self.racine)
        pg = store_pg.PostgresSource(self.url)
        self.assertEqual(pg.liens(), FileSource(self.racine).liens())
        self.assertEqual(pg.liens(), attendu_racine_liens())
        self.assertEqual(pg.liens("60"), FileSource(self.racine).liens("60"))
        self.assertEqual(pg.liens("80"), [])
        self.assertEqual(pg.liens("99"), [])

    def test_republier_ne_duplique_rien(self):
        self.publier(self.racine)
        self.publier(self.racine)
        self.assertEqual(store_pg.PostgresSource(self.url).liens(), attendu_racine_liens())
        self.assertEqual(self.executer("SELECT count(*) FROM publications")[0][0], 3)   # 01, 60, 80

    def test_republier_remplace_les_liens_obsoletes(self):
        self.publier(self.racine)
        csvio.write_rows(DeptPaths(self.racine, "60").links_found,
                         [{"code_insee": "60001", "site_url": "https://a.fr/", "page_url": "https://a.fr/",
                           "type_lien": "lien"}])
        self.publier(self.racine)
        self.assertEqual([r["code_insee"] for r in store_pg.PostgresSource(self.url).liens("60")], ["60001"])

    def test_json_invalide_ignore_sans_exception(self):
        self.publier(self.racine)
        self.executer("UPDATE publications SET liens_json = '{pas du json' WHERE dept = '60'")
        pg = store_pg.PostgresSource(self.url)
        self.assertEqual(pg.liens("60"), [])
        self.assertEqual(pg.liens(), [r for r in attendu_racine_liens() if r["dept"] == "01"])

    def test_json_non_liste_ignore(self):
        self.publier(self.racine)
        for brut in ('{"a": 1}', '"texte"', "42", "null", "true", ""):
            with self.subTest(brut=brut):
                self.executer("UPDATE publications SET liens_json = %s WHERE dept = '60'", (brut,))
                self.assertEqual(store_pg.PostgresSource(self.url).liens("60"), [])

    def test_entrees_mal_formees_ignorees_ou_normalisees(self):
        self.publier(self.racine)
        brut = ('[1, "x", null, {"code_insee": "60009", "url_page": "https://z.example/"}, '
                '{"nom_commune": "Sans code"}]')
        self.executer("UPDATE publications SET liens_json = %s WHERE dept = '60'", (brut,))
        self.assertEqual(store_pg.PostgresSource(self.url).liens("60"), [
            {"dept": "60", "code_insee": "60009", "nom_commune": "", "url_page": "https://z.example/"},
            {"dept": "60", "code_insee": "", "nom_commune": "Sans code", "url_page": ""},
        ])

    def test_noms_accents_guillemets_et_antislash_aller_retour(self):
        racine = self.racine / "accents"
        nom = 'Château-Thierry "du Nord" – L\'Été \\ C:'
        ecrire_dept(racine, "02", [("02001", nom)], [("02001", "https://ct.example/?a=1&b=\"2\"")])
        self.publier(racine)
        ligne = store_pg.PostgresSource(self.url).liens("02")
        self.assertEqual(ligne, FileSource(racine).liens("02"))
        self.assertEqual(ligne[0]["nom_commune"], nom)
        self.assertEqual(ligne[0]["url_page"], "https://ct.example/?a=1&b=\"2\"")

    def test_publication_partielle_n_ecrit_que_le_departement_demande(self):
        self.publier(self.racine, only=["01"])
        pg = store_pg.PostgresSource(self.url)
        self.assertEqual(pg.liens("01"), FileSource(self.racine).liens("01"))
        self.assertEqual(pg.liens("60"), [])

    def test_ancien_schema_mis_a_niveau_puis_lisible(self):
        """Base créée avant la colonne liens_json : publish doit la créer, sans perte des lignes existantes."""
        ancien = "mlm_test_" + uuid.uuid4().hex[:12]
        with psycopg.connect(self.url_base, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {ancien}")
        self.addCleanup(sp._supprimer_schema, self.url_base, ancien)
        sep = "&" if "?" in self.url_base else "?"
        url_ancien = self.url_base + sep + "options=-csearch_path%3D" + ancien
        with psycopg.connect(url_ancien, autocommit=True) as conn:
            conn.execute("CREATE TABLE publications (dept TEXT PRIMARY KEY, controle_date TEXT NOT NULL DEFAULT '', "
                         "communes INTEGER NOT NULL DEFAULT 0, avec_lien INTEGER NOT NULL DEFAULT 0, "
                         "report_html TEXT NOT NULL DEFAULT '', csv_bytes BYTEA NOT NULL DEFAULT ''::bytea, "
                         "csv_present BOOLEAN NOT NULL DEFAULT FALSE, resume_changements TEXT NOT NULL DEFAULT '', "
                         "publie_le TIMESTAMPTZ NOT NULL DEFAULT now())")
            conn.execute("INSERT INTO publications (dept, controle_date) VALUES ('99', '2026-01-01T00:00:00+00:00')")
        with psycopg.connect(url_ancien, autocommit=True) as conn:
            avant = conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = %s "
                                 "AND table_name = 'publications' AND column_name = 'liens_json'", (ancien,)).fetchone()
        self.assertEqual(avant[0], 0)

        self.publier(self.racine, url=url_ancien)

        with psycopg.connect(url_ancien, autocommit=True) as conn:
            apres = conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = %s "
                                 "AND table_name = 'publications' AND column_name = 'liens_json'", (ancien,)).fetchone()
        self.assertEqual(apres[0], 1)
        pg = store_pg.PostgresSource(url_ancien)
        self.assertEqual(pg.depts(), ["01", "60", "80", "99"])
        self.assertEqual(pg.liens(), attendu_racine_liens())
        self.assertIsNotNone(pg.get("99"))                       # ligne d'avant la mise à niveau toujours là
        self.assertEqual(pg.liens("99"), [])                     # liens_json par défaut : aucune commune


if __name__ == "__main__":
    unittest.main()
