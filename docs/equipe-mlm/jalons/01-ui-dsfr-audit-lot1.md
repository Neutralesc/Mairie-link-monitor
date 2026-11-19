# Jalon 01 — Interface DSFR, audit, correctifs lot 1

Date : 2026-11-19. Branche `claude/keen-rubin-fne43o`. Tests : 183 OK.

## Fait
- V0 : DSFR 1.15.4 hors ligne dans `src/static/dsfr/` (le « logo » vosquestions est le bloc-marque DSFR en texte).
- V1b : refonte DSFR de `report.py` / `webapp.py` ; route `/static/dsfr/` sans authentification, protégée contre `..`.
- V3b : audit RGAA, 5 constats corrigés (actualisation contrôlée par l'utilisateur, libellés de liens, captions, titres, liens externes).
- V1a : audit fiabilité, 19 points.
- V2 lot 1 : exceptions isolées par site, timeout sans page lue = erreur_acces, NXDOMAIN non retenté.

## Reste à faire (audit)
- Rappel : points 2, 3, 7, 9, 11, 13 (même fichier `crawl_links.py`, missions en séquence). Points 4, 8, 12 plus lourds.
- Débit : 16, 17, 18, 19.
- **En attente de décision utilisateur (politesse)** : 1 (robots 401/403 = tout interdit) et 6 (parseur robots non conforme RFC 9309).
- QA : tests des correctifs lot 1 (testeur-qa), aucun test ajouté pour l'instant.

## Vercel (décision utilisateur : crawl déclenché depuis Vercel)
Design à proposer : lots serverless, stockage externe (Postgres/KV), API de suivi. Pas de branche `main` distante : PR impossible pour l'instant.
