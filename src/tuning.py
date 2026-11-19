"""Lecture des mesures de performance et recommandation de réglage du parallélisme.

Règles explicites (aucune « magie ») ; elles ne remplacent pas un essai comparatif sur un petit département.
Une exploration a deux phases : la PHASE PLEINE (la file contient encore des sites : le pool de tâches travaille à plein)
et la TRAÎNE (la file est vide, on attend quelques sites lents). Le parallélisme n'agit que sur la phase pleine ; la traîne
se règle autrement (durée maximale par site, plusieurs départements ensemble). Les statistiques de charge sont donc
calculées sur la phase pleine uniquement.
"""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import csvio

TRANSIENT = ("timeout", "erreur_connexion", "limite_429_503")
MAX_PER_PROCESS = 100


def load(path: Path) -> list[dict]:
    rows = csvio.read_rows(path)
    for r in rows:
        for k, v in list(r.items()):
            if k != "horodatage":
                try:
                    r[k] = float(v)
                except (TypeError, ValueError):
                    pass
    return rows


def _pct(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))] if sorted_values else 0.0


def _window(rows: list[dict]) -> float:
    """Durée d'une fenêtre de mesure (médiane des écarts de t_s par processus ; 10 s par défaut)."""
    gaps = []
    by: dict = {}
    for r in rows:
        by.setdefault(r["processus"], []).append(r["t_s"])
    for ts in by.values():
        ts.sort()
        gaps += [b - a for a, b in zip(ts, ts[1:]) if b > a]
    gaps.sort()
    return gaps[len(gaps) // 2] if gaps else 10.0


def split_phases(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(lignes de la phase pleine, lignes de la traîne). Colonne file_attente_min si présente, sinon débit >= 50 % du pic."""
    active = [r for r in rows if r.get("requetes", 0) > 0]
    if active and all("file_attente_min" in r and isinstance(r["file_attente_min"], float) for r in active):
        busy = [r for r in active if r["file_attente_min"] > 0]
    else:
        win = _window(active)
        rate: dict[int, float] = {}
        for r in active:
            rate[int(r["t_s"] // win)] = rate.get(int(r["t_s"] // win), 0.0) + r["requetes"] / win
        peak = max(rate.values(), default=0.0)
        busy_keys = {k for k, v in rate.items() if v >= 0.5 * peak}
        busy = [r for r in active if int(r["t_s"] // win) in busy_keys]
    ids = {id(r) for r in busy}
    return busy, [r for r in active if id(r) not in ids]


def stats(rows: list[dict]) -> dict:
    active = [r for r in rows if r.get("requetes", 0) > 0]
    if not active:
        return {}
    busy, tail = split_phases(rows)
    base = busy or active
    win = _window(active)
    total_req = sum(r["requetes"] for r in active)
    n = sum(r["requetes"] for r in base)
    tot = lambda k, rs=base: sum(r.get(k, 0) for r in rs)  # noqa: E731
    cpu = sorted(r["cpu_pct"] for r in base)
    lag = sorted(r["retard_boucle_p95_ms"] for r in base)
    duree = max(max(r["t_s"] for r in active), 0.1)          # exploration très courte : évite toute division par zéro
    busy_s = len({int(r["t_s"] // win) for r in busy}) * win if busy else 0.0
    configured = int(active[0]["taches_configurees"])
    pool = [r["sites_en_cours_moy"] for r in base] if "sites_en_cours_moy" in base[0] else [r["en_vol_moy"] for r in base]
    mb = [r["debit_mo_s"] for r in base] if "debit_mo_s" in base[0] and isinstance(base[0]["debit_mo_s"], float) else []
    return {
        "processus": len({r["processus"] for r in active}), "duree_s": round(duree, 1), "requetes": int(total_req),
        "phase_pleine_s": round(min(busy_s, duree), 1), "traine_s": round(max(duree - busy_s, 0.0), 1),
        "part_temps_traine": round(max(duree - busy_s, 0.0) / duree, 2) if duree else 0.0,
        "part_requetes_traine": round(sum(r["requetes"] for r in tail) / total_req, 3) if total_req else 0.0,
        "requetes_par_s_phase_pleine": round(n / busy_s, 1) if busy_s else round(total_req / duree, 1),
        "taux_ok": round(tot("ok") / n, 3), "taux_echec_passager": round(sum(tot(k) for k in TRANSIENT) / n, 4),
        "taux_timeout": round(tot("timeout") / n, 4), "taux_429_503": round(tot("limite_429_503") / n, 4),
        "taux_http_4xx": round(tot("http_4xx") / n, 3), "taux_erreur_dns": round(tot("erreur_dns") / n, 3),
        "cpu_pct_moyen": round(sum(cpu) / len(cpu), 1), "cpu_pct_p95": _pct(cpu, .95),
        "retard_boucle_p95_ms": _pct(lag, .95), "latence_p50_ms": _pct(sorted(r["latence_p50_ms"] for r in base), .5),
        "latence_p95_ms": _pct(sorted(r["latence_p95_ms"] for r in base), .95),
        "latence_p95_traine_ms": max((r["latence_p95_ms"] for r in tail), default=0.0),
        "ancien_format": "file_attente_min" not in active[0],
        "taches_configurees_par_processus": configured,
        "saturation_du_pool": round(sum(pool) / len(pool) / configured, 2) if configured else 0.0,
        "debit_mo_s_phase_pleine": round(sum(mb) and (sum(r["debit_mo_s"] for r in base) / max(len({int(r["t_s"] // win) for r in base}), 1)), 2) if mb else None}


def recommend(st: dict, cpu_count: int | None = None, concurrency: int | None = None, max_per_process: int = MAX_PER_PROCESS) -> list[str]:
    if not st:
        return ["Pas assez de mesures (aucune requête enregistrée)."]
    cpu_count = cpu_count or os.cpu_count() or 1
    procs, per = st["processus"], st["taches_configurees_par_processus"]
    capacity = procs * min(per, max_per_process)
    out: list[str] = []
    cpu_bound = st["retard_boucle_p95_ms"] > 200 or st["cpu_pct_p95"] > 85
    if st["taux_echec_passager"] > 0.05 or st["taux_429_503"] > 0.02:
        out.append(f"Trop de requêtes pour la connexion ou les sites : {100 * st['taux_echec_passager']:.1f} % d'échecs passagers en phase pleine "
                   f"(timeouts {100 * st['taux_timeout']:.1f} %, 429/503 {100 * st['taux_429_503']:.1f} %). "
                   "Baisser `parallelisme` d'environ 30 % puis comparer.")
    elif cpu_bound:
        if procs < cpu_count - 1:
            out.append(f"Le processeur est saturé en phase pleine (CPU p95 {st['cpu_pct_p95']:.0f} %, retard de boucle "
                       f"{st['retard_boucle_p95_ms']:.0f} ms) avec {procs} processus pour {cpu_count} cœurs : augmenter `processus`.")
        else:
            out.append(f"Le processeur est saturé en phase pleine (CPU p95 {st['cpu_pct_p95']:.0f} %, retard de boucle "
                       f"{st['retard_boucle_p95_ms']:.0f} ms) et tous les cœurs utiles sont employés : plus de requêtes ne sera pas plus rapide.")
    elif st["saturation_du_pool"] >= 0.85:
        margin = (f"CPU p95 {st['cpu_pct_p95']:.0f} %, retard de boucle {st['retard_boucle_p95_ms']:.0f} ms, "
                  f"échecs passagers {100 * st['taux_echec_passager']:.2f} %")
        target = min(round(procs * per * 1.5 / 10) * 10, procs * max_per_process)
        if per >= max_per_process:
            more = max(cpu_count - 2 - procs, 0)
            out.append(f"Le pool de tâches est saturé ({100 * st['saturation_du_pool']:.0f} % occupé) et il reste de la marge ({margin}) : le "
                       f"plafond de {max_per_process} tâches/processus est atteint ; pour aller plus vite, augmenter `processus` "
                       f"(jusqu'à {procs + more} possible sur {cpu_count} cœurs) puis comparer le débit.")
        else:
            out.append(f"Le pool de tâches est saturé ({100 * st['saturation_du_pool']:.0f} % occupé) et il reste de la marge ({margin}) : "
                       f"augmenter `parallelisme` par paliers (≈ {target} au prochain essai, au plus {max_per_process * procs}) puis "
                       "comparer le débit et le taux d'échecs. Le gain est borné par la politesse (1 requête/s par site).")
    else:
        out.append(f"En phase pleine le pool n'est occupé qu'à {100 * st['saturation_du_pool']:.0f} % : la limite n'est pas le parallélisme "
                   "mais la politesse (1 requête/s par site) ou le nombre de sites à traiter. Augmenter `parallelisme` n'accélérera pas.")
    if st["part_temps_traine"] > 0.4:
        out.append(f"TRAÎNE : {100 * st['part_temps_traine']:.0f} % du temps ({st['traine_s']:.0f} s) pour seulement "
                   f"{100 * st['part_requetes_traine']:.0f} % des requêtes : la file est vide et on attend quelques sites lents (latence p95 "
                   f"{st['latence_p95_traine_ms'] / 1000:.0f} s). Plus de parallélisme n'y change rien. Leviers : lancer plusieurs départements ensemble "
                   "(les traînes se chevauchent), baisser `duree_max_par_site_s` pour une première passe rapide puis relancer en reprise avec une "
                   "valeur plus haute (seuls les sites coupés seront refaits).")
    if st.get("debit_mo_s_phase_pleine"):
        mbit = st["debit_mo_s_phase_pleine"] * 8
        out.append(f"Débit descendant moyen en phase pleine : {st['debit_mo_s_phase_pleine']:.1f} Mo/s ≈ {mbit:.0f} Mbit/s. S'il approche votre "
                   "débit réel, la connexion est le facteur limitant, pas le processeur.")
    if st.get("ancien_format"):
        out.append("Fichier de mesures d'une ancienne version : la latence et les requêtes « en vol » incluent l'attente de politesse "
                   "(délai par site) et le débit réseau n'est pas enregistré. Relancez avec la version actuelle pour des mesures complètes.")
    if st["taux_erreur_dns"] > 0.02:
        out.append(f"{100 * st['taux_erreur_dns']:.1f} % d'erreurs DNS en phase pleine : au-delà de quelques dixièmes de pour cent, "
                   "c'est en général le serveur DNS (box ou fournisseur) qui sature, pas des domaines inexistants. Baisser "
                   "`resolutions_dns_simultanees` (ou `parallelisme`), puis relancer en reprise : les sites en erreur DNS sont refaits.")
    out.append(f"Rappel : {100 * st['taux_http_4xx']:.0f} % de réponses 4xx sont des pages inexistantes, pas un effet de la charge.")
    return out


def machine_info() -> dict:
    return {"os": platform.platform(), "python": platform.python_version(), "cpu_count": os.cpu_count(),
            "processeur": platform.processor()}


def report(path: Path) -> int:
    if not path.exists():
        print(f"Fichier introuvable : {path}")
        return 1
    st = stats(load(path))
    settings, cpu_count = {}, None
    info = path.with_suffix(".json")
    if info.exists():
        try:
            meta = json.loads(info.read_text(encoding="utf-8"))
            settings, cpu_count = meta.get("reglages", {}), meta.get("machine", {}).get("cpu_count")
        except (OSError, ValueError):
            pass
    print(f"\nMesures : {path}")
    for k, v in st.items():
        print(f"  {k:36s} {v}")
    print("\nRecommandation :")
    for line in recommend(st, cpu_count, settings.get("concurrency"), int(settings.get("max_par_processus") or MAX_PER_PROCESS)):
        print(f"  - {line}")
    return 0
