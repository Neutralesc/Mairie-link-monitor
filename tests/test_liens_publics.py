"""Tableau public « Tous les liens trouvés » (accueil) et export /resultats.csv : fichiers et Postgres.

Même contrat pour les deux sources (mixin ContratLiensPublics) : tableau sous les départements, une ligne par lien
confirmé, pagination, filtres `q` et `dep`, échappement, liens non http sans lien, en-têtes, CSV neutralisé et sans
authentification. Sans MLM_TEST_DATABASE_URL, la variante Postgres est ignorée. Aucun réseau.
"""
import csv
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
import csvio  # noqa: E402
import history  # noqa: E402
import liens_vus  # noqa: E402
import store_pg  # noqa: E402
import webapp  # noqa: E402
from layout import DeptPaths  # noqa: E402
from publications import (FileSource, grouper_communes, liens_confirmes, normaliser_page,  # noqa: E402
                          selection_communes, selection_nouveaux)
from test_consultation import MOT_DE_PASSE, ServeurLecture  # noqa: E402

CIBLE = "https://vosquestions.ecologie-territoires.gouv.fr/question"
FORMULE = '=HYPERLINK("https://evil.example","clic")'
HOSTILE = '"><script>alert(1)</script>'
FORMULES = ("=", "+", "-", "@", "\t", "\r")
# 60 : 101 communes « Commune 001 » à « Commune 101 » + « Double » (deux pages source) = 103 liens
# 80 : une commune à formule tableur (page javascript:, non liée) et « Amiens » = 2 liens
# 02 : dossier publié sans inventaire (synthèse vide, ignorée).
# Une ligne par COMMUNE (un seul lien retenu) : 104 communes, deux pages de 100 communes.
TOTAL = 104
COMPTE = f"{TOTAL} communes"


def ligne(code: str, page: str) -> dict:
    return {"code_insee": code, "site_url": "https://site.example/", "page_url": page, "type_lien": "lien",
            "target_url": CIBLE, "texte_lien": "Vos questions"}


def construire_racine_liens(root: Path) -> None:
    p60 = DeptPaths(root, "60")
    p60.reports_dir.mkdir(parents=True)
    communes = [{"code_insee": f"60{n:03d}", "nom_commune": f"Commune {n:03d}", "nb_urls_valides": "1"}
                for n in range(1, 102)]
    communes.append({"code_insee": "60900", "nom_commune": "Double", "nb_urls_valides": "1"})
    csvio.write_rows(p60.inventory_communes, communes)
    liens = [ligne(f"60{n:03d}", f"https://c{n}.example/") for n in range(1, 102)]
    liens += [ligne("60900", "https://double.example/"), ligne("60900", "https://double.example/contact")]
    csvio.write_rows(p60.links_found, liens)

    p80 = DeptPaths(root, "80")
    p80.reports_dir.mkdir(parents=True)
    csvio.write_rows(p80.inventory_communes, [
        {"code_insee": "80001", "nom_commune": FORMULE, "nb_urls_valides": "1"},
        {"code_insee": "80002", "nom_commune": "Amiens", "nb_urls_valides": "1"},
    ])
    csvio.write_rows(p80.links_found, [ligne("80001", "javascript:alert(1)"), ligne("80002", "https://amiens.example/")])

    (root / "reports" / "02").mkdir(parents=True)
    for p in (p60, p80):
        history.record(p, history.db_path(root))


def ecrire_commune(root: Path, dept: str, code: str, nom: str, page: str) -> None:
    """Ajoute une commune avec un lien confirmé (crée le département si besoin) : simule un nouveau contrôle."""
    p = DeptPaths(root, dept)
    p.reports_dir.mkdir(parents=True, exist_ok=True)
    communes = csvio.read_rows(p.inventory_communes) if p.inventory_communes.exists() else []
    if not any(c["code_insee"] == code for c in communes):
        communes.append({"code_insee": code, "nom_commune": nom, "nb_urls_valides": "1"})
        csvio.write_rows(p.inventory_communes, communes)
    liens = csvio.read_rows(p.links_found) if p.links_found.exists() else []
    liens.append(ligne(code, page))
    csvio.write_rows(p.links_found, liens)


def retirer_liens(root: Path, dept: str, code: str) -> None:
    p = DeptPaths(root, dept)
    csvio.write_rows(p.links_found, [l for l in csvio.read_rows(p.links_found) if l["code_insee"] != code])


HOSTILE_NOM = "<img src=x onerror=alert(1)>"


def cellules(corps: str) -> list[list[str]]:
    """Lignes CSV (séparateur « ; ») sans BOM."""
    return list(csv.reader(io.StringIO(corps.lstrip("﻿")), delimiter=";"))


class ContratLiensPublics:
    """Contrat commun aux variantes. Utilise self.appel et self.html, fournis par ServeurLecture."""

    def section(self, html: str) -> str:
        return html[html.index('id="tous-les-liens"'):]

    def test_tableau_sous_les_departements(self):
        html = self.html("/")
        dept = html.index("<caption>Dernière publication")
        liens = html.index('id="tous-les-liens"')
        self.assertLess(dept, liens)
        self.assertIn("<caption>Liens trouvés par commune", html)
        self.assertGreater(html.index("<caption>Liens trouvés par commune"), liens)

    def test_colonnes_et_une_ligne_par_lien(self):
        html = self.html("/")
        for entete in ("Département", "Code INSEE", "Nom de la commune",
                       "Page de la commune où se trouve le lien vers Vos questions",
                       "Lien vers vosquestions", "Apparu le"):
            self.assertIn(f'<th scope="col">{entete}</th>', html)
        self.assertIn(f"{COMPTE}, page 1 sur 2.", html)
        self.assertIn("<td>Oise (60)</td>", html)
        self.assertIn("<td>60001</td>", html)
        seconde = self.html("/?page=2")  # « Double » est en fin de département, donc sur la page 2
        self.assertIn("<td>Double</td>", seconde)
        self.assertIn('href="https://double.example/"', seconde)
        self.assertEqual(seconde.count("<td>Double</td>"), 1)  # une seule ligne pour la commune
        self.assertNotIn('href="https://double.example/contact"', seconde)  # un seul lien retenu

    def test_pagination_cent_lignes_par_page(self):
        premiere = self.html("/")
        self.assertIn('rel="next"', premiere)
        self.assertNotIn('rel="prev"', premiere)
        self.assertIn('aria-label="Page suivante, page 2"', premiere)
        self.assertIn('aria-current="page"', premiere)
        self.assertIn("<td>Commune 100</td>", premiere)
        self.assertNotIn("<td>Commune 101</td>", premiere)
        seconde = self.html("/?page=2")
        self.assertIn('rel="prev"', seconde)
        self.assertNotIn('rel="next"', seconde)
        self.assertIn(f"{COMPTE}, page 2 sur 2.", seconde)
        self.assertIn("<td>Commune 101</td>", seconde)
        self.assertNotIn("<td>Commune 100</td>", seconde)
        self.assertIn('href="/?page=1#tous-les-liens"', seconde)  # liens ancrés sur le titre de section

    def test_page_hors_plage_ou_invalide(self):
        self.assertIn("page 2 sur 2", self.html("/?page=9"))          # au-delà : dernière page
        for brut in ("0", "-3", "abc", "", "99999999999999999999"):
            with self.subTest(page=brut):
                statut, _, corps = self.appel("GET", "/?page=" + brut)
                self.assertEqual(statut, 200)
                self.assertIn(f"{COMPTE}, page 1 sur 2.", corps)

    def test_filtre_departement(self):
        html = self.html("/?dep=80")
        self.assertIn("2 communes, page 1 sur 1.", html)
        self.assertIn("<td>Amiens</td>", html)
        self.assertNotIn("<td>Commune 001</td>", html)
        self.assertNotIn('aria-label="Pagination des liens"', html)
        self.assertIn('<option value="80" selected>', html)
        self.assertIn('href="/#tous-les-liens">Réinitialiser', html)

    def test_filtre_recherche_commune_et_insee(self):
        self.assertIn("1 commune, page 1 sur 1.", self.html("/?q=Commune%20007"))
        self.assertIn("1 commune, page 1 sur 1.", self.html("/?q=60900"))   # code INSEE
        self.assertIn("1 commune, page 1 sur 1.", self.html("/?q=double"))  # sans casse
        self.assertIn("Aucun résultat pour cette recherche.", self.html("/?q=double&dep=80"))

    def test_departement_invalide_ignore_avec_alerte(self):
        for dep in ("99", "2a", "2A", "%3Cscript%3E", "123456"):
            with self.subTest(dep=dep):
                statut, _, corps = self.appel("GET", "/?dep=" + dep)
                self.assertEqual(statut, 200)
                self.assertIn("Département inconnu", corps)
                self.assertIn(f"{COMPTE}, page 1 sur 2.", corps)
                self.assertNotIn("<script>alert", corps)

    def test_recherche_hostile_echappee(self):
        import urllib.parse
        corps = self.html("/?q=" + urllib.parse.quote(HOSTILE) + "&dep=" + urllib.parse.quote(HOSTILE))
        self.assertNotIn("<script>alert(1)", corps)
        self.assertIn("&lt;script&gt;alert(1)", corps)
        self.assertIn("Aucun résultat pour cette recherche.", corps)

    def test_url_non_http_affichee_sans_lien(self):
        html = self.html("/?dep=80")
        self.assertNotIn('href="javascript', html.lower())
        self.assertIn('<span title="javascript:alert(1)">javascript:alert(1)</span>', html)
        self.assertNotIn('href="https://evil.example', html)

    def test_rel_et_nouvelle_fenetre_des_liens_externes(self):
        balises = [b for b in self.section(self.html("/")).split("<a ") if 'target="_blank"' in b]
        self.assertTrue(balises)
        for balise in balises:
            self.assertIn('rel="noopener noreferrer nofollow"', balise)
        self.assertIn("(nouvelle fenêtre)", self.section(self.html("/")))

    def test_pas_de_lien_vers_routes_protegees(self):
        section = self.section(self.html("/"))
        for protegee in ('href="/analyse"', 'href="/suivi"', 'href="/rapports', 'href="/campagnes', 'href="/resultats?'):
            self.assertNotIn(protegee, section)

    def test_etat_vide(self):
        html = self.html("/?q=zzz-rien")
        self.assertIn("Aucun résultat pour cette recherche.", html)
        self.assertIn("<thead><tr>", self.section(html))  # en-tête conservé même sans résultat
        self.assertIn('<tbody><tr><td colspan="6">Aucun résultat.</td></tr></tbody>', self.section(html))

    def test_une_seule_titre_1_et_titre_de_section(self):
        html = self.html("/")
        self.assertEqual(len(webapp.re.findall(r"<h1[\s>]", html)), 1)
        self.assertIn('<h2 class="fr-h4 fr-mt-6w" id="tous-les-liens" tabindex="-1">Tous les liens trouvés</h2>', html)
        self.assertIn('action="/#tous-les-liens"', html)

    def test_entete_resultats_toujours_present(self):
        """Bandeau de synthèse, tableau par département et son en-tête restent en haut de la page, quoi qu'on demande."""
        for chemin in ("/", "/?page=2", "/?q=zzz-rien", "/?dep=99", "/?dep=60&q=zzz-rien", "/?page=abc", "/?dep=80"):
            with self.subTest(chemin=chemin):
                html = self.html(chemin)
                self.assertIn("Chiffres clés", html)
                self.assertIn("<caption>Dernière publication de chaque département", html)
                self.assertIn('<th scope="col">Dernière publication</th>', html)
                self.assertIn("<thead><tr>", html)
                self.assertLess(html.index("<caption>Dernière publication"), html.index('id="tous-les-liens"'))

    def test_entete_avec_identifiants_administrateur(self):
        """Avec le mot de passe, « / » affiche les mêmes résultats que pour un visiteur, plus le menu complet ; aucun
        lancement dans la vue résultats. L'ancien tableau de bord (/tableau-de-bord) est redirigé vers l'accueil."""
        html = self.html("/", auth=MOT_DE_PASSE)
        self.assertIn("Chiffres clés", html)
        self.assertIn('id="tous-les-liens"', html)
        self.assertNotIn('href="/tableau-de-bord"', html)
        self.assertIn('href="/suivi"', html)                    # menu administrateur
        self.assertNotIn("Lancer une analyse", html)
        self.assertNotIn('method="post"', html.lower())
        self.assertNotIn("Espace réservé", html)
        self.assertEqual(self.appel("GET", "/tableau-de-bord", auth=MOT_DE_PASSE)[0], 308)

    def test_csv_entete_lignes_et_en_tetes(self):
        statut, en_tetes, corps = self.appel("GET", "/resultats.csv")
        self.assertEqual(statut, 200)
        self.assertTrue(en_tetes.get("Content-Type", "").startswith("text/csv"))
        self.assertIn('filename="liens_confirmes.csv"', en_tetes.get("Content-Disposition", ""))
        self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertEqual(en_tetes.get("Cache-Control"), "no-store")
        self.assertIn("Authorization", en_tetes.get("Vary", ""))
        lignes = cellules(corps)
        self.assertEqual(lignes[0], ["departement_code", "departement", "code_insee", "commune", "page_source",
                                     "url_cible", "apparu_le"])
        self.assertEqual(len(lignes) - 1, TOTAL)  # une ligne par commune
        self.assertIn(["60", "Oise", "60900", "Double", "https://double.example/", CIBLE], [l[:6] for l in lignes])
        self.assertTrue(all(l[6] == "" or "T" in l[6] for l in lignes[1:]))  # date ISO ou vide (base sans date)
        self.assertFalse(any(l[0] == "02" for l in lignes[1:]))  # dossier sans synthèse : ignoré

    def test_csv_neutralise(self):
        lignes = cellules(self.appel("GET", "/resultats.csv?dep=80")[2])
        commune = [c for ligne_ in lignes[1:] for c in ligne_ if "HYPERLINK" in c]
        self.assertEqual(commune, ["'" + FORMULE])
        for ligne_ in lignes:
            for c in ligne_:
                self.assertFalse(c.startswith(FORMULES), c)

    def test_csv_filtres(self):
        self.assertEqual(len(cellules(self.appel("GET", "/resultats.csv?dep=80")[2])) - 1, 2)
        self.assertEqual(len(cellules(self.appel("GET", "/resultats.csv?q=60900")[2])) - 1, 1)
        self.assertEqual(len(cellules(self.appel("GET", "/resultats.csv?q=Commune%20007")[2])) - 1, 1)
        self.assertEqual(len(cellules(self.appel("GET", "/resultats.csv?dep=02")[2])) - 1, 0)  # publié, sans lien
        self.assertEqual(len(cellules(self.appel("GET", "/resultats.csv?dep=99")[2])) - 1, TOTAL)  # filtre ignoré

    def test_csv_sans_authentification_et_verbes(self):
        self.assertEqual(self.appel("GET", "/resultats.csv")[0], 200)
        for methode in ("POST", "PUT", "DELETE"):
            with self.subTest(methode=methode):
                statut, en_tetes, _ = self.appel(methode, "/resultats.csv", auth=MOT_DE_PASSE, origine=True,
                                                 corps=b"dept=60")
                self.assertEqual(statut, 405)
                self.assertEqual(en_tetes.get("Allow"), "GET, HEAD")
        statut, en_tetes, corps = self.appel("HEAD", "/resultats.csv")
        self.assertEqual((statut, corps), (200, ""))
        self.assertGreater(int(en_tetes.get("Content-Length", "0")), 0)


class ContratNouveaux:
    """Liens nouveaux (mémoire des premières apparitions). Hooks fournis par chaque variante : republier,
    reinitialiser_table, nombre_lignes_vues."""

    def apparu_le(self, code: str) -> str:
        return [l[6] for l in cellules(self.appel("GET", "/resultats.csv")[2])[1:] if l[2] == code][0]

    def section_nouveaux(self, html: str) -> str:
        return html[html.index('id="nouveaux-liens"'):html.index('id="tous-les-liens"')]

    def test_cycle_des_nouveautes(self):
        # amorçage : rien de nouveau tant que rien n'a changé
        self.assertIn("Aucune nouvelle commune ces 30 derniers jours.", self.section_nouveaux(self.html("/")))
        # un lien de plus, avec un nom hostile : une nouveauté, échappée
        ecrire_commune(self.root, "80", "80003", HOSTILE_NOM, "https://nouvelle.example/")
        self.republier()
        html = self.html("/")
        self.assertIn("1 commune avec un nouveau lien depuis 30 jours.", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", self.section_nouveaux(html))
        self.assertNotIn("<img src=x", html)
        self.assertIn("(nouvelle fenêtre)", self.section_nouveaux(html))
        premiere = self.apparu_le("80003")
        self.assertTrue(premiere)
        # republication identique : aucune nouveauté de plus, aucune ligne de plus
        lignes = self.nombre_lignes_vues()
        self.republier()
        self.assertEqual(self.nombre_lignes_vues(), lignes)
        self.assertIn("1 commune avec un nouveau lien depuis 30 jours.", self.html("/"))
        # lien disparu puis réapparu : la première date est gardée
        retirer_liens(self.root, "80", "80003")
        self.republier()
        ecrire_commune(self.root, "80", "80003", HOSTILE_NOM, "https://nouvelle.example/")
        self.republier()
        self.assertEqual(self.apparu_le("80003"), premiere)
        # département jamais publié : tous ses liens sont nouveaux
        ecrire_commune(self.root, "27", "27001", "Evreux", "https://evreux.example/")
        ecrire_commune(self.root, "27", "27002", "Vernon", "https://vernon.example/")
        self.republier()
        self.assertIn("3 communes avec un nouveau lien depuis 30 jours.", self.html("/"))

    def test_hors_fenetre_absent_de_la_section(self):
        self.html("/")  # amorçage (première lecture) avant le nouveau lien
        ecrire_commune(self.root, "80", "80003", "Nouvelle", "https://nouvelle.example/")
        self.republier()
        self.assertIn("1 commune avec un nouveau lien depuis 30 jours.", self.html("/"))
        plus_tard = datetime.now(timezone.utc) + timedelta(days=31)
        with mock.patch.object(webapp, "horloge", lambda: plus_tard):
            html = self.html("/")
        self.assertIn("Aucune nouvelle commune ces 30 derniers jours.", self.section_nouveaux(html))
        self.assertNotIn(">nouveau</span>", html)  # repère absent du tableau complet aussi

    def test_migration_sans_table_amorcee_sans_nouveau(self):
        ecrire_commune(self.root, "80", "80003", "Nouvelle", "https://nouvelle.example/")
        self.republier()
        self.reinitialiser_table()  # base sans liens_vus : l'existant (dont 80003) devient la base
        self.assertIn("Aucune nouvelle commune ces 30 derniers jours.", self.section_nouveaux(self.html("/")))
        self.assertGreater(self.nombre_lignes_vues(), 0)

    def test_repere_nouveau_dans_le_tableau_complet(self):
        self.html("/")  # amorçage
        ecrire_commune(self.root, "80", "80003", "Nouvelle", "https://nouvelle.example/")
        self.republier()
        html = self.html("/?page=2")  # le département 80 est en fin de tri : page 2
        self.assertIn('<th scope="col">Apparu le</th>', html)
        self.assertIn('fr-badge--no-icon">nouveau</span>', html)  # texte, pas seulement la couleur

    def test_entetes_toujours_presentes_avec_nouveautes(self):
        self.html("/")  # amorçage
        ecrire_commune(self.root, "80", "80003", "Nouvelle", "https://nouvelle.example/")
        self.republier()
        for chemin in ("/", "/?page=2", "/?q=zzz-rien"):
            with self.subTest(chemin=chemin):
                html = self.html(chemin)
                self.assertIn("Chiffres clés", html)
                self.assertIn('<th scope="col">Dernière publication</th>', html)
                self.assertIn('<th scope="col">Apparu le</th>', html)


class LiensFichiers(ContratLiensPublics, ContratNouveaux, ServeurLecture):
    lecture = True

    def preparer(self, root: Path) -> None:
        construire_racine_liens(root)

    def republier(self) -> None:
        """Pas de publication explicite en local : chaque lecture enregistre les liens nouvellement vus."""
        self.html("/")

    def reinitialiser_table(self) -> None:
        liens_vus.oublier(history.db_path(self.root))

    def nombre_lignes_vues(self) -> int:
        import sqlite3
        with sqlite3.connect(history.db_path(self.root)) as conn:
            return conn.execute("SELECT count(*) FROM liens_vus").fetchone()[0]


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class LiensPostgres(ContratLiensPublics, ContratNouveaux, ServeurLecture, sp.PgSchemaTestCase):
    lecture = True

    def preparer(self, root: Path) -> None:
        construire_racine_liens(root)
        self.vider_tables()
        conn = store_pg.connect(self.url)
        try:
            store_pg.publish(FileSource(root), conn)  # la ligne « 02 » est publiée avec synthese_json = '{}'
            conn.execute("UPDATE liens_vus SET nouveau = FALSE")  # amorçage : l'existant n'est pas nouveau
            conn.commit()
        finally:
            conn.close()

    def setUp(self):
        patch = mock.patch.object(store_pg, "CACHE_LEGER_S", 0.0)  # pas de cache : les republications sont visibles
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def creer_source(self):
        return store_pg.PostgresSource(self.url)

    def republier(self) -> None:
        conn = store_pg.connect(self.url)
        try:
            store_pg.publish(FileSource(self.root), conn)
        finally:
            conn.close()

    def reinitialiser_table(self) -> None:
        import psycopg
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DROP TABLE liens_vus")
        conn = store_pg.connect(self.url)
        try:
            store_pg.ensure_schema(conn)  # recrée la table et l'amorce avec les publications existantes
        finally:
            conn.close()

    def nombre_lignes_vues(self) -> int:
        import psycopg
        with psycopg.connect(self.url) as conn:
            return conn.execute("SELECT count(*) FROM liens_vus").fetchone()[0]

    def test_publications_concurrentes_pas_de_doublon(self):
        import psycopg
        self.republier()
        avant = self.nombre_lignes_vues()
        self.republier()
        self.assertEqual(self.nombre_lignes_vues(), avant)
        with psycopg.connect(self.url) as conn, self.assertRaises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO liens_vus (departement, code_insee, url_page_source, url_cible, commune) "
                         "SELECT departement, code_insee, url_page_source, url_cible, commune FROM liens_vus LIMIT 1")


class LiensLectureDesactivee(ServeurLecture):
    lecture = False

    def preparer(self, root: Path) -> None:
        construire_racine_liens(root)

    def test_csv_protege_sans_lecture_publique(self):
        self.assertEqual(self.appel("GET", "/resultats.csv")[0], 401)
        self.assertEqual(self.appel("GET", "/", auth=None)[0], 401)


class HelpersLiens(unittest.TestCase):
    def synthese(self):
        return {"communes": [
            {"code_insee": "1", "nom": "Ok", "statut": "lien_confirme", "liens_confirmes": [
                {"url_page_source": "https://a/", "url_cible": "https://v/", "verification": None},
                {"url_page_source": "https://a/", "url_cible": "https://v/", "verification": None},
                {"url_page_source": "https://a/2", "url_cible": "https://v/", "verification": "faux"},
                {"url_page_source": "", "url_cible": "https://v/", "verification": None},
            ]},
            {"code_insee": "2", "nom": "Incertaine", "statut": "lien_incertain", "liens_confirmes": [
                {"url_page_source": "https://b/", "url_cible": "https://v/", "verification": None}]},
            {"code_insee": "3", "nom": "Casse", "statut": "lien_confirme", "liens_confirmes": "pas une liste"},
            "pas un objet",
        ]}

    def test_liens_confirmes_filtre_et_dedoublonne(self):
        lignes = liens_confirmes("60", self.synthese())
        self.assertEqual(lignes, [{"dept": "60", "code_insee": "1", "nom_commune": "Ok",
                                   "url_page_source": "https://a/", "url_cible": "https://v/"}])

    def test_synthese_vide_ou_mal_formee(self):
        for synth in ({}, None, {"communes": None}, [], {"communes": [1, "x"]}):
            with self.subTest(synth=synth):
                self.assertEqual(liens_confirmes("60", synth), [])

    def test_selection_sans_accents_et_tranche(self):
        lignes = [{"dept": "78", "code_insee": "78172", "nom_commune": "Élancourt", "url_page_source": "https://e/",
                   "url_cible": ""},
                  {"dept": "60", "code_insee": "60001", "nom_commune": "Abbeville", "url_page_source": "https://a/",
                   "url_cible": ""}]
        tranche, total = selection_communes(lignes, "elancourt", 10, 0)
        self.assertEqual((total, [r["code_insee"] for r in tranche]), (1, ["78172"]))
        tranche, total = selection_communes(lignes, "", 1, 1)
        self.assertEqual((total, [r["code_insee"] for r in tranche]), (2, ["78172"]))  # tri : Abbeville puis Élancourt

    def test_parametres_publics(self):
        params = {"q": ["x" * 150], "dep": [" 80 "], "page": ["3"]}
        q, dep, alerte, page = webapp.parametres_liens(params, ["60", "80"])
        self.assertEqual((len(q), dep, alerte, page), (100, "80", "", 3))
        _, dep, alerte, _ = webapp.parametres_liens({"dep": ["99"]}, ["60", "80"])
        self.assertEqual((dep, bool(alerte)), ("", True))
        self.assertEqual(webapp.parametres_liens({"page": ["1234567"]}, [])[3], 1)

    def test_pagination_liens_ancree(self):
        html = webapp.pagination_liens("a&b", "60", 2, 3)
        self.assertIn('rel="prev"', html)
        self.assertIn('rel="next"', html)
        self.assertIn("q=a%26b", html)  # valeur encodée dans l'adresse
        self.assertIn("#tous-les-liens", html)


CIBLE_COMMUNE = "https://vosquestions.ecologie-territoires.gouv.fr/35040-breteil"
CIBLE_RACINE = "https://vosquestions.ecologie-territoires.gouv.fr/?utm_source=chatgpt.com"
PAGE_BRETEIL = "https://www.breteil.bzh/actualite/les-reponses-a-vos-questions-a-breteil"


def lien(code: str, page: str, cible: str) -> dict:
    return {"code_insee": code, "site_url": "https://site.example/", "page_url": page, "type_lien": "lien",
            "target_url": cible, "texte_lien": "Vos questions"}


def construire_regroupement(root: Path) -> None:
    """35 : Breteil (même page, deux cibles) et Saint-Test (quatre écritures d'une même page) ;
    81 : 80 communes, 70 avec deux pages, 10 avec une (150 pages) ; 82 : 101 communes à une page chacune."""
    def departement(code: str, communes: list[tuple[str, str]], liens: list[dict]) -> None:
        p = DeptPaths(root, code)
        p.reports_dir.mkdir(parents=True)
        csvio.write_rows(p.inventory_communes, [{"code_insee": c, "nom_commune": n, "nb_urls_valides": "1"}
                                                for c, n in communes])
        csvio.write_rows(p.links_found, liens)
        history.record(p, history.db_path(root))

    departement("35", [("35040", "Breteil"), ("35041", "Saint-Test")], [
        lien("35040", PAGE_BRETEIL, CIBLE_COMMUNE), lien("35040", PAGE_BRETEIL, CIBLE_RACINE),
        lien("35041", "https://saint-test.fr/", CIBLE_COMMUNE),
        lien("35041", "http://www.saint-test.fr/?utm_source=x#top", CIBLE_COMMUNE),
        lien("35041", "https://saint-test.fr", CIBLE_COMMUNE)])
    communes81 = [(f"81{n:03d}", f"Ville 81-{n:03d}") for n in range(1, 81)]
    liens81 = []
    for n in range(1, 81):
        liens81.append(lien(f"81{n:03d}", f"https://ville81-{n:03d}.example/a", CIBLE_COMMUNE))
        if n <= 70:
            liens81.append(lien(f"81{n:03d}", f"https://ville81-{n:03d}.example/b", CIBLE_COMMUNE))
    departement("81", communes81, liens81)
    communes82 = [(f"82{n:03d}", f"Ville 82-{n:03d}") for n in range(1, 102)]
    departement("82", communes82, [lien(c, f"https://ville82-{c[2:]}.example/", CIBLE_COMMUNE) for c, _ in communes82])


class RegroupementCommunes(ServeurLecture):
    """Une ligne par commune (un seul lien retenu) : pagination et compteur en communes, CSV par commune."""

    lecture = True

    def preparer(self, root: Path) -> None:
        construire_regroupement(root)

    def test_breteil_une_ligne_cible_propre(self):
        html = self.html("/?dep=35&q=breteil")
        self.assertEqual(html.count("<td>Breteil</td>"), 1)
        self.assertEqual(html.count(f'href="{PAGE_BRETEIL}"'), 1)
        self.assertIn(f'href="{CIBLE_COMMUNE}"', html)           # cible propre à la commune retenue
        self.assertNotIn(f'href="{CIBLE_RACINE}"', html)         # la racine avec utm n'est pas affichée
        self.assertIn("1 commune, page 1 sur 1.", html)
        lignes = cellules(self.appel("GET", "/resultats.csv?dep=35&q=breteil")[2])
        self.assertEqual(len(lignes), 2)                         # en-tête + une commune
        self.assertEqual(lignes[1][4], PAGE_BRETEIL)
        self.assertEqual(lignes[1][5], CIBLE_COMMUNE)

    def test_urls_equivalentes_une_seule_page(self):
        lignes = cellules(self.appel("GET", "/resultats.csv?dep=35&q=saint-test")[2])
        self.assertEqual(len(lignes), 2)
        self.assertEqual(lignes[1][4], "https://saint-test.fr")  # une page, forme https la plus courte
        self.assertEqual(lignes[1][5], CIBLE_COMMUNE)

    def test_pagination_en_communes(self):
        premiere = self.html("/?dep=82")
        self.assertIn("101 communes, page 1 sur 2.", premiere)
        self.assertEqual(premiere.count("<td>Ville 82-"), 100)
        seconde = self.html("/?dep=82&page=2")
        self.assertIn("101 communes, page 2 sur 2.", seconde)
        self.assertEqual(seconde.count("<td>Ville 82-"), 1)

    def test_compteur_en_communes(self):
        self.assertIn("80 communes, page 1 sur 1.", self.html("/?dep=81"))
        self.assertIn("9 communes, page 1 sur 1.", self.html("/?dep=81&q=Ville%2081-00"))
        self.assertIn("1 commune, page 1 sur 1.", self.html("/?dep=81&q=Ville%2081-001"))

    def test_filtre_departement_et_recherche_par_code(self):
        self.assertIn("1 commune, page 1 sur 1.", self.html("/?q=35040"))
        self.assertIn("Aucun résultat pour cette recherche.", self.html("/?dep=35&q=81001"))

    def test_csv_une_ligne_par_commune_neutralisee(self):
        lignes = cellules(self.appel("GET", "/resultats.csv?dep=81")[2])
        self.assertEqual(len(lignes) - 1, 80)
        deux = [l for l in lignes[1:] if l[3] == "Ville 81-001"][0]
        self.assertEqual(deux[4], "https://ville81-001.example/a")  # deux pages : la plus propre, alphabétique
        for ligne_ in lignes:
            for c in ligne_:
                self.assertFalse(c.startswith(FORMULES), c)


def lien_test(code: str, page: str, cible: str, vue: str, nouveau: bool = False, nom: str = "A") -> dict:
    return {"dept": "60", "code_insee": code, "nom_commune": nom, "url_page_source": page, "url_cible": cible,
            "premiere_vue": vue, "nouveau": nouveau}


class NormalisationEtGroupes(unittest.TestCase):
    def test_normaliser_page_equivalences(self):
        self.assertEqual(normaliser_page("http://www.A.fr:80/x/?utm_source=a&b=1#f"), normaliser_page("https://a.fr/x?b=1"))
        self.assertEqual(normaliser_page("https://a.fr/"), normaliser_page("https://a.fr"))
        self.assertEqual(normaliser_page("https://a.fr/?fbclid=1&gclid=2&q=3"), "https://a.fr/?q=3")
        self.assertEqual(normaliser_page("https://a.fr/x/"), "https://a.fr/x")
        self.assertNotEqual(normaliser_page("https://a.fr/x"), normaliser_page("https://a.fr/y"))
        self.assertEqual(normaliser_page("javascript:alert(1)"), "javascript:alert(1)")  # pas de fusion abusive

    def test_breteil_un_lien_retenu_apparu_le_le_plus_ancien(self):
        liens = [lien_test("35040", PAGE_BRETEIL, CIBLE_COMMUNE, "2026-11-02T10:00:00+00:00", nom="Breteil"),
                 lien_test("35040", PAGE_BRETEIL, CIBLE_RACINE, "2026-11-01T10:00:00+00:00", True, nom="Breteil")]
        communes = grouper_communes(liens)
        self.assertEqual(len(communes), 1)
        self.assertEqual((communes[0]["page"], communes[0]["cible"]), (PAGE_BRETEIL, CIBLE_COMMUNE))
        self.assertEqual(communes[0]["apparu_le"], "2026-11-01T10:00:00+00:00")  # la plus ancienne, tous liens confondus
        self.assertTrue(communes[0]["nouveau"])

    def test_deux_pages_la_plus_propre_puis_la_plus_courte(self):
        liens = [lien_test("60001", "https://a.fr/long/page?utm_source=x", CIBLE_COMMUNE, "2026-11-01T10:00:00+00:00"),
                 lien_test("60001", "http://a.fr/p", CIBLE_COMMUNE, "2026-11-01T10:00:00+00:00"),
                 lien_test("60001", "https://a.fr/p", CIBLE_COMMUNE, "2026-11-02T10:00:00+00:00")]
        self.assertEqual(grouper_communes(liens)[0]["page"], "https://a.fr/p")
        self.assertEqual(grouper_communes(list(reversed(liens)))[0]["page"], "https://a.fr/p")  # déterministe

    def test_deux_cibles_generiques_la_plus_propre(self):
        liens = [lien_test("60001", "https://a.fr/p", CIBLE_RACINE, "2026-11-01T10:00:00+00:00"),
                 lien_test("60001", "https://a.fr/p", "https://vosquestions.ecologie-territoires.gouv.fr/",
                           "2026-11-02T10:00:00+00:00")]
        self.assertEqual(grouper_communes(liens)[0]["cible"], "https://vosquestions.ecologie-territoires.gouv.fr/")

    def test_nouveaux_une_ligne_par_commune(self):
        maintenant = datetime.now(timezone.utc)
        recent = (maintenant - timedelta(days=1)).isoformat()
        ancien = (maintenant - timedelta(days=90)).isoformat()
        liens = [lien_test("60001", "https://a.fr/old", CIBLE_COMMUNE, ancien),
                 lien_test("60001", "https://a.fr/new2", CIBLE_COMMUNE, recent, True),
                 lien_test("60001", "https://a.fr/new1", CIBLE_COMMUNE, recent, True)]
        tranche, total = selection_nouveaux(liens, maintenant - timedelta(days=30), 10)
        self.assertEqual(total, 1)
        self.assertEqual(tranche[0]["page"], "https://a.fr/new1")
        self.assertEqual(tranche[0]["apparu_le"], recent)


if __name__ == "__main__":
    unittest.main()
