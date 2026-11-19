"""Runner d'analyse GitHub Actions (actions_runner) avec un faux store en mémoire et un faux client HTTP. Sans Postgres."""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications  # noqa: E402,F401  (chemins src/ et tests/)

import actions_runner  # noqa: E402
from actions_runner import MSG_DB, MSG_DEJA, MSG_RESEAU, ActionsRunner  # noqa: E402

TOKEN = "jeton-de-test-0123456789"
URL_BASE = "postgresql://utilisateur-test@hote-test:5433/base-test"
DISPATCH = "https://api.github.com/repos/proprio/depot/actions/workflows/analyse.yml/dispatches"


class FauxConnexion:
    def close(self):
        pass


class FauxStore:
    """Imite les fonctions de store_pg utilisées par ActionsRunner, en mémoire."""

    def __init__(self):
        self.lignes: dict[int, dict] = {}
        self.suivant = 1
        self.panne = False
        self.connexions = 0

    def _verifier(self):
        if self.panne:
            raise RuntimeError("base injoignable")

    def connect(self, url):
        self._verifier()
        self.connexions += 1
        return FauxConnexion()

    def ajouter(self, statut="en_cours", debut=None, run_id="", journal="", depts="60,80", reprise=False):
        aid = self.suivant
        self.suivant += 1
        self.lignes[aid] = {"id": aid, "departements": depts, "reprise": reprise, "statut": statut,
                            "debut": debut or datetime.now(timezone.utc), "fin": None, "journal": journal,
                            "run_id": run_id}
        return aid

    def analysis_current(self, conn):
        self._verifier()
        if not self.lignes:
            return None
        return dict(self.lignes[max(self.lignes)])

    def analysis_queue(self, conn, depts, reprise):
        self._verifier()
        if any(r["statut"] in ("file", "en_cours") for r in self.lignes.values()):
            return None
        return self.ajouter("file", depts=",".join(depts), reprise=reprise)

    def analysis_end(self, conn, aid, statut):
        self._verifier()
        r = self.lignes.get(aid)
        if r is None or r["statut"] not in ("file", "en_cours"):
            return False
        r["statut"], r["fin"] = statut, datetime.now(timezone.utc)
        return True

    def statut(self, aid):
        return self.lignes[aid]["statut"]


class FauxHttp:
    """Enregistre chaque appel et renvoie la réponse programmée (statut, corps) ou lève l'exception programmée."""

    def __init__(self, reponse=(204, b""), exception=None):
        self.reponse = reponse
        self.exception = exception
        self.appels: list[dict] = []

    def __call__(self, method, url, headers, body):
        self.appels.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self.exception is not None:
            raise self.exception
        return self.reponse


class ActionsRunnerTests(unittest.TestCase):
    def setUp(self):
        self.store = FauxStore()
        self.http = FauxHttp()
        # Pas de cache pendant les tests (sauf ceux qui le testent) : chaque lecture voit l'état du faux store.
        patch = mock.patch.object(actions_runner, "CACHE_S", 0.0)
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, repo="proprio/depot", token=TOKEN, ref="main", workflow="analyse.yml", http=None):
        return ActionsRunner(URL_BASE, repo, token, ref, workflow, http=http or self.http, store=self.store)


class DispatchTests(ActionsRunnerTests):
    def test_start_dispatch_204_retourne_message_vide(self):
        self.assertEqual(self.runner().start(["60", "80"], False), "")
        self.assertEqual(len(self.http.appels), 1)
        appel = self.http.appels[0]
        self.assertEqual((appel["method"], appel["url"]), ("POST", DISPATCH))

    def test_corps_json_du_dispatch(self):
        self.runner(ref="dev").start(["60", "2A"], True)
        corps = json.loads(self.http.appels[0]["body"].decode("utf-8"))
        aid = self.store.lignes[1]["id"]
        self.assertEqual(corps, {"ref": "dev", "inputs": {"depts": "60 2A", "reprise": "true",
                                                          "analysis_id": str(aid)}})

    def test_reprise_faux_dans_le_corps(self):
        self.runner().start(["01"], False)
        self.assertEqual(json.loads(self.http.appels[0]["body"])["inputs"]["reprise"], "false")

    def test_en_tetes_sans_fuite_et_complets(self):
        self.runner().start(["60"], False)
        h = self.http.appels[0]["headers"]
        self.assertEqual(h["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(h["Accept"], "application/vnd.github+json")
        self.assertEqual(h["X-GitHub-Api-Version"], "2022-11-28")
        self.assertEqual(h["User-Agent"], "mairie-link-monitor")
        self.assertEqual(h["Content-Type"], "application/json")

    def test_dispatch_marque_l_analyse_en_file(self):
        self.runner().start(["60"], False)
        self.assertEqual(self.store.statut(1), "file")
        self.assertTrue(self.runner().running())

    def test_analyse_deja_active_message_exact(self):
        self.store.ajouter("en_cours")
        self.assertEqual(self.runner().start(["60"], False),
                         "Une analyse est déjà en cours : attendez la fin ou arrêtez-la.")
        self.assertEqual(self.http.appels, [])

    def test_message_analyse_active_constante(self):
        self.assertEqual(MSG_DEJA, "Une analyse est déjà en cours : attendez la fin ou arrêtez-la.")

    def test_dispatch_non_204_termine_en_echec(self):
        self.http.reponse = (422, b'{"message": "Validation Failed"}')
        message = self.runner().start(["60"], False)
        self.assertEqual(message, "Impossible de lancer l'analyse (GitHub : HTTP 422).")
        self.assertEqual(self.store.statut(1), "echec")
        self.assertFalse(self.runner().running())

    def test_dispatch_404_message_avec_code(self):
        self.http.reponse = (404, b"")
        self.assertIn("HTTP 404", self.runner().start(["60"], False))
        self.assertEqual(self.store.statut(1), "echec")

    def test_exception_reseau_message_dedie(self):
        self.http = FauxHttp(exception=OSError("connexion refusée"))
        with self.assertLogs("actions_runner", level="WARNING"):
            self.assertEqual(self.runner().start(["60"], False), MSG_RESEAU)
        self.assertEqual(self.store.statut(1), "echec")

    def test_base_en_panne_au_demarrage(self):
        self.store.panne = True
        with self.assertLogs("actions_runner", level="WARNING"):
            self.assertEqual(self.runner().start(["60"], False), MSG_DB)
        self.assertEqual(self.http.appels, [])

    def test_jeton_absent_des_messages_et_des_journaux(self):
        self.http = FauxHttp(exception=OSError(f"echec avec {TOKEN} dans le message"))
        with self.assertLogs(level="DEBUG") as journal:
            message = self.runner().start(["60"], False)
            self.http.exception = None
            self.http.reponse = (500, TOKEN.encode())
            self.store.lignes.clear()
            message2 = self.runner().start(["80"], False)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(TOKEN, message2)
        self.assertTrue(journal.records)
        self.assertFalse(any(TOKEN in r.getMessage() for r in journal.records))

    def test_deuxieme_demarrage_refuse_pendant_la_file(self):
        self.runner().start(["60"], False)
        self.assertEqual(self.runner().start(["80"], False), MSG_DEJA)
        self.assertEqual(len(self.http.appels), 1)


class PeremptionTests(ActionsRunnerTests):
    def test_analyse_recente_reste_active(self):
        self.store.ajouter("en_cours", debut=datetime.now(timezone.utc) - timedelta(hours=6))
        self.assertTrue(self.runner().running())

    def test_analyse_de_plus_de_sept_heures_perimee(self):
        aid = self.store.ajouter("en_cours", debut=datetime.now(timezone.utc) - timedelta(hours=7, minutes=1))
        self.assertFalse(self.runner().running())
        self.assertEqual(self.store.statut(aid), "echec")

    def test_perimee_ne_bloque_pas_un_nouveau_demarrage(self):
        aid = self.store.ajouter("file", debut=datetime.now(timezone.utc) - timedelta(hours=8))
        self.assertEqual(self.runner().start(["60"], False), "")
        self.assertEqual(self.store.statut(aid), "echec")
        self.assertEqual(self.store.statut(2), "file")

    def test_pas_d_analyse_pas_de_blocage(self):
        self.assertFalse(self.runner().running())
        self.assertEqual(self.runner().label, "")
        self.assertEqual(self.runner().tail(), "")
        self.assertEqual(self.runner().started, 0.0)


class LectureEtatTests(ActionsRunnerTests):
    def test_label_et_reprise(self):
        self.store.ajouter("en_cours", depts="60,2A", reprise=True)
        self.assertEqual(self.runner().label, "60 2A (reprise)")

    def test_label_sans_reprise(self):
        self.store.ajouter("en_cours", depts="80")
        self.assertEqual(self.runner().label, "80")

    def test_tail_garde_les_dernieres_lignes(self):
        self.store.ajouter("en_cours", journal="a\nb\nc\nd\n")
        r = self.runner()
        self.assertEqual(r.tail(2), "c\nd")
        self.assertEqual(r.tail(0), "")
        self.assertEqual(r.tail(), "a\nb\nc\nd")

    def test_started_est_l_horodatage_du_debut(self):
        debut = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
        self.store.ajouter("en_cours", debut=debut)
        self.assertEqual(self.runner().started, debut.timestamp())

    def test_cache_de_deux_secondes(self):
        self.store.ajouter("en_cours")
        with mock.patch.object(actions_runner, "CACHE_S", 60.0):
            r = self.runner()
            connexions = self.store.connexions
            self.assertTrue(r.running())
            self.assertEqual(r.label, "60 80")
            self.assertEqual(self.store.connexions, connexions + 1)   # une seule lecture, ensuite le cache

    def test_base_en_panne_pas_d_exception(self):
        self.store.ajouter("en_cours", journal="x\n")
        self.store.panne = True
        r = self.runner()
        with self.assertLogs("actions_runner", level="WARNING") as logs:
            self.assertFalse(r.running())
            self.assertEqual(r.label, "")
            self.assertEqual(r.tail(), "")
            self.assertEqual(r.started, 0.0)
            r.stop()
        self.assertTrue(logs.records)
        texte = "\n".join(x.getMessage() for x in logs.records)
        self.assertIn("RuntimeError", texte)                     # seul le type d'erreur est journalisé
        self.assertNotIn("base injoignable", texte)


class StopTests(ActionsRunnerTests):
    def test_stop_sans_run_id_marque_annulee(self):
        aid = self.store.ajouter("en_cours", run_id="")
        self.runner().stop()
        self.assertEqual(self.store.statut(aid), "annulee")
        self.assertEqual(self.http.appels, [])

    def test_stop_avec_run_id_demande_l_annulation(self):
        aid = self.store.ajouter("en_cours", run_id="98765")
        self.http.reponse = (202, b"")
        self.runner().stop()
        appel = self.http.appels[0]
        self.assertEqual((appel["method"], appel["url"]),
                         ("POST", "https://api.github.com/repos/proprio/depot/actions/runs/98765/cancel"))
        self.assertEqual(self.store.statut(aid), "en_cours")   # le statut final viendra du runner

    def test_stop_refuse_par_github_journalise_sans_exception(self):
        self.store.ajouter("en_cours", run_id="1")
        self.http.reponse = (409, b"")
        with self.assertLogs("actions_runner", level="WARNING") as logs:
            self.runner().stop()
        self.assertIn("409", logs.output[0])

    def test_stop_sans_analyse_ne_fait_rien(self):
        self.runner().stop()
        self.assertEqual(self.http.appels, [])

    def test_stop_analyse_deja_finale_ne_fait_rien(self):
        aid = self.store.ajouter("terminee")
        self.runner().stop()
        self.assertEqual(self.store.statut(aid), "terminee")
        self.assertEqual(self.http.appels, [])

    def test_stop_exception_reseau_journalisee(self):
        self.store.ajouter("en_cours", run_id="7")
        self.http = FauxHttp(exception=OSError("hors ligne"))
        with self.assertLogs("actions_runner", level="WARNING") as logs:
            self.runner().stop()
        self.assertIn("OSError", logs.output[0])


class ValidationTests(unittest.TestCase):
    def build(self, repo="proprio/depot", ref="main", workflow="analyse.yml"):
        return ActionsRunner(URL_BASE, repo, TOKEN, ref, workflow, http=FauxHttp(), store=FauxStore())

    def test_configuration_valide_acceptee(self):
        self.assertIsInstance(self.build(), ActionsRunner)
        self.assertIsInstance(self.build(ref="feature/x-1.2", workflow="ci_analyse.yaml"), ActionsRunner)
        self.assertIsInstance(self.build(repo="Proprio.Nom/depot-2_v1"), ActionsRunner)

    def test_depot_invalide_refuse(self):
        for repo in ("sans-barre", "a/b/c", "../depot", "proprio/", "/depot", "proprio/dépôt", "proprio/d epot",
                     "proprio/.."):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                self.build(repo=repo)

    def test_workflow_invalide_refuse(self):
        for workflow in ("dir/analyse.yml", "..", "analyse yml", "", "analyse;rm.yml"):
            with self.subTest(workflow=workflow), self.assertRaises(ValueError):
                self.build(workflow=workflow)

    def test_ref_invalide_refusee(self):
        for ref in ("a..b", "/main", "main/", "x y", "", "../main"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.build(ref=ref)

    def test_depot_non_chaine_refuse(self):
        with self.assertRaises(ValueError):
            self.build(repo=None)


# ----------------------------------------------------------------- correctifs de fiabilité (lot B)

class _StoreAnnulable(FauxStore):
    """Ajoute analysis_cancel_unstarted : annule une analyse 'file' sans run_id, comme store_pg."""

    def analysis_cancel_unstarted(self, conn, aid):
        self._verifier()
        r = self.lignes.get(aid)
        if r is None or r["statut"] != "file" or r["run_id"]:
            return False
        r["statut"], r["fin"] = "annulee", datetime.now(timezone.utc)
        return True


class _StoreCourse(FauxStore):
    """Course simulée : le CI s'attache (run_id 555) juste avant l'annulation conditionnelle, qui échoue."""

    def analysis_cancel_unstarted(self, conn, aid):
        self._verifier()
        self.lignes[aid]["run_id"] = "555"
        return False


class _StoreTerminee(FauxStore):
    """Course simulée : l'analyse atteint un statut final juste avant l'annulation conditionnelle, qui échoue."""

    def analysis_cancel_unstarted(self, conn, aid):
        self._verifier()
        self.lignes[aid]["statut"] = "terminee"
        return False


class StopCourseTests(ActionsRunnerTests):
    def test_stop_file_sans_run_id_annulee_sans_appel_github(self):
        self.store = _StoreAnnulable()
        aid = self.store.ajouter("file", run_id="")
        self.runner().stop()
        self.assertEqual(self.store.statut(aid), "annulee")
        self.assertEqual(self.http.appels, [])

    def test_stop_course_le_ci_s_attache_puis_run_annule(self):
        self.store = _StoreCourse()
        aid = self.store.ajouter("file", run_id="")
        self.http.reponse = (202, b"")
        self.runner().stop()
        self.assertEqual(len(self.http.appels), 1)
        appel = self.http.appels[0]
        self.assertEqual((appel["method"], appel["url"]),
                         ("POST", "https://api.github.com/repos/proprio/depot/actions/runs/555/cancel"))
        self.assertEqual(self.store.statut(aid), "file")        # le statut final viendra du runner

    def test_stop_course_analyse_finale_entre_temps_rien_a_faire(self):
        self.store = _StoreTerminee()
        aid = self.store.ajouter("file", run_id="")
        self.runner().stop()
        self.assertEqual(self.store.statut(aid), "terminee")
        self.assertEqual(self.http.appels, [])


class PeremptionFileTests(ActionsRunnerTests):
    """Analyse 'file' : périmée au-delà de 30 minutes (les 6 h et 7 h de 'en_cours' sont dans PeremptionTests)."""

    def test_file_de_31_minutes_perimee_et_close_en_echec(self):
        aid = self.store.ajouter("file", debut=datetime.now(timezone.utc) - timedelta(minutes=31))
        self.assertFalse(self.runner().running())
        self.assertEqual(self.store.statut(aid), "echec")

    def test_file_de_29_minutes_toujours_active(self):
        aid = self.store.ajouter("file", debut=datetime.now(timezone.utc) - timedelta(minutes=29))
        self.assertTrue(self.runner().running())
        self.assertEqual(self.store.statut(aid), "file")


if __name__ == "__main__":
    unittest.main()
