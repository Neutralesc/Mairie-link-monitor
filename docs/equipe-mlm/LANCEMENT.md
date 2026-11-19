# Équipe mlm — mise en place et utilisation

## Création des agents
Créés par team-architect dans `.claude/agents/` (contenu dans `AGENTS.md`) :
- `developpeur-backend` — optimisation du crawler — haiku
- `analyste-fiabilite` — audit erreurs et accès — haiku
- `developpeur-frontend` — refonte DSFR — haiku
- `expert-accessibilite` — revue RGAA/DSFR — haiku
- `testeur-qa` — tests et non-régression — haiku

Ce sont des fichiers, pas des conversations. `/agents` les liste. Redémarrage de la session nécessaire (`claude --continue`) car `.claude/agents/` n'existait pas au démarrage.

## Lancement
Tout passe par `/team-architect` :
- `/team-architect plan à blanc` — montre les vagues sans rien lancer.
- `/team-architect` — reprend à partir du dernier jalon.

team-architect vous sollicitera seulement pour : logo du ministère indisponible, changement de politique de politesse du crawler, publication.

## Tests
1. Plan à blanc : ordre et découpage sensés.
2. Une mission isolée de la vague 1 : format du compte rendu, fichiers respectés.
3. Blocage simulé : l'agent répond BLOQUÉ, team-architect cherche avant d'escalader.
4. Premier jalon : synthèse écrite.

## Hypothèses
- Le paquet npm `@gouvfr/dsfr` est accessible ; sinon le DSFR est à fournir.
- Le logo du ministère (vosquestions, 503 au moment de la conception) est récupérable plus tard ; sinon bloc-marque République Française seul.

## Limites
- Pas d'accès au site de documentation du DSFR (403) : composants repris du paquet npm.
- Pas de test sur de vrais sites de mairies sans autorisation réseau adaptée.

## Désinstallation
Supprimez les fichiers d'agents de l'équipe (`.claude/agents/<nom>.md`, liste dans `AGENTS.md`), puis retirez de `CLAUDE.md` le bloc entre `<!-- team-architect:mlm:debut -->` et `<!-- team-architect:mlm:fin -->`. `docs/equipe-mlm/` peut être gardé comme archive.
