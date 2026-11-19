# Design : interface sur Vercel, crawl dans GitHub Actions

Statut : **validé par l'utilisateur** (plan Vercel Hobby ; crawl hors Vercel, tant que c'est gratuit ; stockage Postgres Neon). Politesse du crawler inchangée (robots.txt RFC 9309, délais par site).

## 1. Pourquoi cette répartition
| Contrainte | Conséquence |
|---|---|
| Vercel Hobby : fonctions courtes, pas de processus d'arrière-plan, disque éphémère | Le crawl ne tourne **pas** sur Vercel. Vercel ne sert que l'interface, en lecture, et déclenche l'analyse. |
| IP Vercel = hébergeur cloud (certains sites de mairies bloquent) | Autre raison de garder le crawl ailleurs. Les IP de GitHub Actions sont aussi des IP cloud : à mesurer, le rappel peut baisser. |
| GitHub Actions : gratuit (minutes illimitées si dépôt public, quota mensuel sinon), un job peut durer jusqu'à 6 h, disque disponible | Le pipeline actuel (fichiers CSV + SQLite) tourne **tel quel** dans un job. Aucun découpage en lots nécessaire. |

## 2. Architecture
```
Navigateur ─ Vercel (api/index.py, DSFR) ─ lit ─▶ Postgres (Neon)
     │ POST /lancer
     └──▶ GitHub API : workflow_dispatch (dépts, reprise) ─▶ GitHub Actions
                         job : python src/mairie_link_monitor.py run …  (fichiers locaux au runner)
                         puis : python src/mairie_link_monitor.py publier ─▶ écrit dans Postgres
```
- **Publication** = par département : compte rendu HTML complet, CSV de résultat, date du contrôle, nombre de communes et de communes avec lien ; plus le CSV global. Postgres ne contient donc que ce que l'interface affiche.
- **État de l'analyse** : table `analyses` (id, départements, statut file/en cours/terminée/échec, début, fin, dernières lignes du journal) mise à jour par le workflow (début, fin, journal toutes les minutes). L'interface lit cette table pour le suivi.
- **Reprise entre deux exécutions** (`--resume`) : le dossier `data/` + `reports/` + `historique.sqlite` est sauvegardé/restauré par `actions/cache` (ou artefact), clé par département.
- **Arrêt** : l'interface demande l'annulation du run via l'API GitHub ; le crawler conserve ses points de sauvegarde (reprise possible).
- **Sécurité** : mot de passe Basic de l'interface (variable `MLM_PASSWORD`, obligatoire en ligne) ; jeton GitHub à portée minimale (déclencher/annuler un workflow de ce dépôt) en variable Vercel ; `DATABASE_URL` en secret GitHub **et** Vercel ; aucun secret dans le dépôt.

## 3. Interface de lecture (migration par couches)
`src/publications.py` : `Publication` + protocole `PublicationSource` (`depts()`, `get(dept)`, `csv_all()`). Deux implémentations :
- `FileSource(root)` : comportement actuel (fichiers + SQLite), usage local et tests.
- `PostgresSource(url)` : lecture des publications dans Neon (Vercel).
`webapp.py` ne lit plus que `source`. Le `Runner` local (processus enfant) reste pour l'usage local ; en ligne il est remplacé par un `ActionsRunner` (déclenche/annule le workflow, lit `analyses`).

## 4. Missions
| Vague | Mission | Agent | Dépend de |
|---|---|---|---|
| S1 | `publications.py` + `FileSource`, branchement de `webapp.py` (sans changement de comportement) | developpeur-backend | — |
| S2 | `publier.py` (fichiers → Postgres, schéma SQL, tables `publications` et `analyses`) + `PostgresSource` (`psycopg[binary]`, chargé seulement en mode Postgres) + sous-commande `publier` | developpeur-backend | S1 |
| S3 | Workflow `.github/workflows/analyse.yml` (dispatch, cache, publication, journal) + `ActionsRunner` (dispatch/annulation via API GitHub, lecture de `analyses`) | developpeur-backend | S2 |
| S4 | Adaptateur Vercel : `api/index.py`, `vercel.json`, `public/static/dsfr` (déplacement des ressources), `requirements.txt` | developpeur-frontend | S2 |
| S5 | Tests : `FileSource` / `PostgresSource` (base Postgres locale de test si disponible, sinon fakes), publication, `ActionsRunner` avec API simulée | testeur-qa | S1–S4 |
| S6 | Revue fiabilité (publication partielle, concurrence de deux analyses, secrets) | analyste-fiabilite | S3 |
| Parallèle | Points de débit de l'audit (16 à 19) dans `crawl_links.py` / `platforms.py` | developpeur-backend | — |

## 5. Risques et points ouverts
- **Rappel depuis des IP cloud** : à mesurer sur un département test après déploiement ; pas mesurable depuis ce bac à sable.
- **Quota Actions** si le dépôt est privé : une analyse de toute la France peut dépasser le quota gratuit mensuel ; analyser par départements ou rendre le dépôt public (à décider par l'utilisateur).
- **Neon gratuit** : stockage limité (publications seules : quelques Mo par département, largement suffisant).
- **Pas de test possible** ici contre un vrai Neon, Vercel ou GitHub Actions : tests locaux avec fakes ; validation finale par l'utilisateur sur un déploiement réel (je fournirai la liste des étapes).
