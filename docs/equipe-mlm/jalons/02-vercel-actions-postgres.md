# Jalon 02 — Interface sur Vercel, crawl dans GitHub Actions, stockage Postgres

Date : 2026-11-19. Branche `claude/keen-rubin-fne43o`. Tests : 362 (avec base Postgres de test), 305 + 57 ignorés sans base.

## Fait
- Crawler : lots 1 à 4 (exceptions isolées, NXDOMAIN, timeout sans page, RFC 9309 + parseur `robots_rfc.py`, pagination, identité de page, sitemap, exécuteur DNS).
- Architecture Vercel (`VERCEL.md`) : S1 `publications.py`, S2 `store_pg.py` + `publier`, S3 `ci_analyse.py` + `.github/workflows/analyse.yml` + `ActionsRunner`, S4 `api/index.py` + `vercel.json`, S5/6 tests et revue de fiabilité (16 points ; corrigés : 1, 3, 4, 5, 6, 7, 8, 10, 12, 13, 14, 15 ; différés : 2 à confirmer au déploiement, 9 et 16 documentés, 11 assumé).
- `DEPLOIEMENT.md` : étapes utilisateur (Neon, secrets GitHub, jeton, variables Vercel).

## Reste à faire
- Déploiement réel par l'utilisateur ; vérifier `self.path` derrière le rewrite Vercel, la péremption des analyses en file (30 min) et le rappel depuis des IP cloud.
- Points de débit 17 à 19 de l'audit (non traités), points de rappel 4, 8, 12.
- Pas de branche `main` distante : pas de PR possible.
