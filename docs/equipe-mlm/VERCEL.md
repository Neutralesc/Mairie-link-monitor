# Design : exécution sur Vercel (crawl déclenché depuis l'interface)

Statut : **proposition, à valider**. Décisions déjà prises par l'utilisateur : crawl déclenché depuis Vercel ; stockage Postgres (Neon via Vercel) ; politesse du crawler inchangée (robots.txt RFC 9309, délais par site).

## 1. Contraintes Vercel qui structurent tout
| Contrainte | Conséquence |
|---|---|
| Fonction = requête/réponse, durée plafonnée (environ 300 s par défaut, davantage selon le plan) | Impossible de faire tourner un crawl de département d'un seul tenant : il faut des **lots courts** enchaînés. |
| Pas de processus d'arrière-plan, pas de `subprocess` durable | Le `Runner` actuel (processus enfant + journal fichier) disparaît. |
| Disque éphémère (`/tmp` seulement, perdu entre invocations) | `data/`, `reports/`, `resultats/` et `historique.sqlite` doivent vivre en **Postgres**. |
| Fichiers statiques servis depuis `public/` | Les ressources DSFR passent de `src/static/dsfr/` à `public/static/dsfr/`. |
| Pas de multi-processus fiable | `--processes` vaut 1 sur Vercel ; la concurrence asyncio (déjà élevée) est conservée. |

## 2. Architecture
```
Navigateur ── /            Interface DSFR (api/index.py, WSGI/handler) ── lecture Postgres
          ── POST /lancer  crée un « job » (départements) en base, répond tout de suite
          ── GET /api/etat avancement (jobs, lots terminés / total)
Vercel Cron (1/min) ou auto-appel ── /api/step  ── traite UN lot puis planifie le suivant
Postgres (Neon) : jobs, lots, communes, urls, resultats_crawl, historique, journal
```
- **Job** = une analyse demandée (départements, reprise). **Lot** = un ensemble borné de sites (ex. 150 sites, calibré pour tenir sous ~240 s).
- `/api/step` prend le lot « à faire » le plus ancien (verrou `FOR UPDATE SKIP LOCKED`), exécute `crawl_links.crawl_all` sur ces sites avec `site_timeout` et durée de lot bornés, écrit les résultats, marque le lot terminé. Si des lots restent, il se ré-invoque (POST authentifié par secret interne) ; Vercel Cron sert de filet de reprise si la chaîne se rompt.
- Étapes d'un job, chacune découpée en lots : inventaire (DILA/COG) → vérification URL → découverte → crawl → plateformes (PanneauPocket) → historique + compte rendu. Les comptes rendus HTML sont **générés à la demande** depuis Postgres (plus de fichiers).
- Le « pool global de requêtes » multi-départements est conservé **dans un lot** ; la politesse par site (Throttle, robots.txt) reste dans le processus du lot. Un même site n'est jamais dans deux lots simultanés d'un job.
- Arrêt d'une analyse : drapeau `job.annule` lu entre deux lots (et par le lot en cours via un contrôle périodique).

## 3. Couche de stockage (la clé de la migration)
Aujourd'hui, `inventory`, `verify_urls`, `discover`, `crawl_links`, `platforms`, `coverage`, `results`, `history` lisent/écrivent des CSV via `layout.DeptPaths` et `csvio`. Plan :
1. Introduire un module `store.py` (interface : lire/écrire les « tables » CSV par département, enregistrer l'historique), avec deux implémentations : **fichiers** (comportement actuel, pour usage local et tests) et **Postgres**.
2. Les modules existants appellent `store` au lieu des chemins directs, sans changer leur logique. Tests existants conservés (backend fichiers).
3. Tables Postgres : une table par CSV d'origine (clé `(dept, code_insee, …)`), table `historique` équivalente au SQLite actuel, tables `jobs`/`lots`/`journal`.
Dépendance ajoutée : `psycopg[binary]` (seulement chargée en mode Postgres).

## 4. Interface
- `webapp.py` : la logique de rendu (`home`, `report.render`) reste ; le serveur `http.server` est conservé pour le local ; un adaptateur `api/index.py` expose les mêmes routes sur Vercel. Authentification Basic conservée, mot de passe en variable d'environnement `MLM_PASSWORD`, **obligatoire** en ligne.
- Suivi : la page interroge `/api/etat` (actualisation contrôlée par l'utilisateur, déjà en place) ; le journal vient de la table `journal`.
- Téléchargement CSV : généré à la volée depuis Postgres.

## 5. Variables d'environnement Vercel
`DATABASE_URL` (Neon), `MLM_PASSWORD`, `MLM_STEP_SECRET` (auto-appel / cron), `MLM_CONTACT` (e-mail du User-Agent), réglages de lot (`MLM_LOT_SITES`, `MLM_LOT_SECONDS`). Aucun secret dans le dépôt.

## 6. Découpage en missions (après validation)
| Vague | Mission | Agent | Dépend de |
|---|---|---|---|
| S1 | `store.py` + backend fichiers (extraction sans changement de comportement) | developpeur-backend | — |
| S2 | Backend Postgres + schéma SQL + migration | developpeur-backend | S1 |
| S3 | Lots et orchestration (`jobs`, `lots`, `api/step`), arrêt/reprise | developpeur-backend | S2 |
| S4 | Adaptateur Vercel (`api/`, `vercel.json`, `public/static/dsfr`, `requirements.txt`) + interface `/api/etat` | developpeur-frontend + backend | S3 |
| S5 | Tests (backend fichiers, Postgres via base de test locale, lots, annulation) | testeur-qa | S1–S4 |
| S6 | Revue fiabilité de l'orchestration (verrous, reprise, doublons) | analyste-fiabilite | S3 |
Chaque mission est courte (haiku) ; j'impose interfaces et schéma SQL avant chaque vague.

## 7. Risques et points ouverts
- **Débit réel** par lot à mesurer (aucune mesure possible depuis ce bac à sable sans crawl réel) : réglage de `MLM_LOT_SITES`.
- **Durée maximale** de fonction selon le plan Vercel de l'utilisateur : à confirmer (détermine la taille des lots).
- **Crawl depuis des IP Vercel** : certains sites bloquent les hébergeurs cloud ; le rappel peut baisser, à mesurer.
- **Inventaire DILA/COG** : téléchargements volumineux ; mis en cache en base.
- Pas de test possible contre un vrai Neon ici : base Postgres locale pour les tests, validation finale sur un déploiement réel par l'utilisateur.
