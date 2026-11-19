"""Mesures de performance de l'exploration : de quoi régler le nombre de requêtes simultanées sur des données.

Par fenêtre (10 s par défaut) et par processus : débit, requêtes en vol, issues (réussites, timeouts, erreurs de connexion,
429/503, 5xx…), latences p50/p95, utilisation CPU du processus et retard de la boucle asyncio (saturation).
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone

FIELDS = ["horodatage", "processus", "t_s", "taches_configurees", "sites_en_cours_moy", "sites_en_cours_max",
          "file_attente_min", "en_vol_moy", "en_vol_max", "attente_politesse_moy", "requetes", "req_par_s", "octets_recus",
          "debit_mo_s",
          "ok", "http_4xx", "http_5xx", "limite_429_503", "timeout", "erreur_connexion", "erreur_dns", "erreur_ssl", "autre",
          "latence_p50_ms", "latence_p95_ms", "cpu_pct", "retard_boucle_p95_ms", "retard_boucle_max_ms", "sites_termines"]
OUTCOMES = ("ok", "http_4xx", "http_5xx", "limite_429_503", "timeout", "erreur_connexion", "erreur_dns", "erreur_ssl", "autre")


def outcome(kind: str, status: int | None) -> str:
    if status in (429, 503):
        return "limite_429_503"
    if kind in ("accessible", "non_html", ""):
        return "ok"
    return kind if kind in OUTCOMES else "autre"


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))]


class Metrics:
    def __init__(self, pid: int, configured: int):
        self.pid, self.configured = pid, configured
        self.inflight = 0          # requêtes réellement en cours sur le réseau (hors attente de politesse)
        self.waiting = 0           # requêtes qui attendent leur tour (délai minimal par site)
        self.active_sites = 0      # sites en cours d'exploration (tâches du pool occupées)
        self.queue_len = 0         # sites encore en file d'attente
        self.bytes = 0
        self.sites_done = 0
        self.start = time.monotonic()
        self._last_t, self._last_cpu = self.start, time.process_time()
        self._reset()

    def _reset(self) -> None:
        self.counts: Counter = Counter()
        self.latencies: list[float] = []
        self.lags: list[float] = []
        self.inflight_samples: list[int] = []
        self.waiting_samples: list[int] = []
        self.active_samples: list[int] = []
        self.queue_samples: list[int] = []
        self.bytes = 0

    def record(self, kind: str, status: int | None, seconds: float) -> None:
        self.counts[outcome(kind, status)] += 1
        if len(self.latencies) < 20000:
            self.latencies.append(seconds * 1000)

    def probe(self, lag_ms: float) -> None:
        self.lags.append(lag_ms)
        self.inflight_samples.append(self.inflight)
        self.waiting_samples.append(self.waiting)
        self.active_samples.append(self.active_sites)
        self.queue_samples.append(self.queue_len)

    def snapshot(self) -> dict:
        now, cpu = time.monotonic(), time.process_time()
        dt = max(now - self._last_t, 1e-6)
        n = sum(self.counts.values())
        avg = lambda xs, cur: round(sum(xs) / len(xs), 1) if xs else cur  # noqa: E731
        row = {"horodatage": datetime.now(timezone.utc).isoformat(timespec="seconds"), "processus": self.pid,
               "t_s": round(now - self.start, 1), "taches_configurees": self.configured,
               "sites_en_cours_moy": avg(self.active_samples, self.active_sites),
               "sites_en_cours_max": max(self.active_samples or [self.active_sites]),
               "file_attente_min": min(self.queue_samples or [self.queue_len]),
               "en_vol_moy": avg(self.inflight_samples, self.inflight), "en_vol_max": max(self.inflight_samples or [self.inflight]),
               "attente_politesse_moy": avg(self.waiting_samples, self.waiting),
               "requetes": n, "req_par_s": round(n / dt, 1), "octets_recus": self.bytes,
               "debit_mo_s": round(self.bytes / dt / 1e6, 2),
               **{k: self.counts.get(k, 0) for k in OUTCOMES},
               "latence_p50_ms": round(percentile(self.latencies, 0.5)), "latence_p95_ms": round(percentile(self.latencies, 0.95)),
               "cpu_pct": round(100 * (cpu - self._last_cpu) / dt, 1),
               "retard_boucle_p95_ms": round(percentile(self.lags, 0.95), 1), "retard_boucle_max_ms": round(max(self.lags or [0.0]), 1),
               "sites_termines": self.sites_done}
        self._last_t, self._last_cpu = now, cpu
        self._reset()
        return row
