# Déploiement de l'interface sur Vercel

Ce guide décrit, pas à pas, la mise en ligne de l'interface « Liens Vos questions ». Il ne contient aucun secret :
chaque valeur sensible est à saisir par vous, dans les tableaux de bord indiqués, jamais dans le dépôt.

Rôles :
- **Vercel** (plan Hobby) sert l'interface et lit la base.
- **Neon** (Postgres) stocke les résultats publiés.
- **GitHub Actions** exécute les analyses (le crawl ne tourne pas sur Vercel).

## 1. Créer la base Neon

Deux possibilités, au choix.

- **Via Vercel** : dans le tableau de bord Vercel, onglet *Storage*, *Create Database*, puis *Neon (Serverless Postgres)*.
  Vercel ajoute alors la variable `DATABASE_URL` au projet (étape 5).
- **Via neon.tech** : créez un projet, puis copiez la chaîne de connexion « Postgres » (elle commence par
  `postgresql://` et contient `sslmode=require`).

Conservez cette chaîne : c'est `DATABASE_URL`. Elle contient un mot de passe ; ne la collez ni dans un fichier du dépôt,
ni dans un ticket, ni dans une conversation.

Le schéma (tables `publications`, `publication_globale`, `analyses`) est créé automatiquement par la première
publication, et par l'interface au premier accès.

## 2. Renseigner GitHub (secrets et variable du dépôt)

Dans le dépôt GitHub, *Settings*, *Secrets and variables*, *Actions* :

- onglet *Secrets*, bouton *New repository secret* : nom `DATABASE_URL`, valeur = la chaîne de l'étape 1 ;
- onglet *Variables*, bouton *New repository variable* : nom `MLM_CONTACT`, valeur = une adresse de contact
  (facultative, mais recommandée : elle est transmise au crawler pour que les mairies puissent vous joindre).

## 3. Créer le jeton GitHub à portée minimale

Le jeton sert à déclencher et annuler le workflow `analyse.yml`, et à rien d'autre.

1. Dans GitHub : votre photo, *Settings*, *Developer settings*, *Personal access tokens*, *Fine-grained tokens*,
   *Generate new token*.
2. *Resource owner* : le compte ou l'organisation qui possède le dépôt.
3. *Expiration* : choisissez une date (notez-la : le jeton expire, il faudra le renouveler).
4. *Repository access* : *Only select repositories*, puis ce dépôt uniquement.
5. *Permissions*, *Repository permissions* : **Actions : Read and write**. Aucune autre permission.
6. Générez le jeton et copiez-le immédiatement (GitHub ne l'affiche qu'une fois).

## 4. Choisir le mot de passe de l'interface

Choisissez un mot de passe long et unique (au moins 16 caractères, aléatoires). Il protège toute l'interface : sans lui,
l'application ne démarre pas (les pages répondent « Configuration incomplète »).

## 5. Renseigner les variables Vercel

Vercel : votre projet, *Settings*, *Environment Variables*. Ajoutez, pour l'environnement *Production* (et *Preview*
si vous voulez tester les branches) :

| Nom | Valeur | Obligatoire |
|---|---|---|
| `DATABASE_URL` | chaîne de connexion Neon (étape 1) | oui |
| `MLM_PASSWORD` | mot de passe de l'interface (étape 4) | oui |
| `MLM_GH_TOKEN` | jeton GitHub (étape 3) | oui |
| `MLM_GH_REPO` | `propriétaire/dépôt`, par exemple `mon-compte/Mairie-link-monitor` | oui |
| `MLM_GH_REF` | branche qui contient le workflow, en général `main` | non (défaut `main`) |

Marquez `MLM_PASSWORD`, `MLM_GH_TOKEN` et `DATABASE_URL` comme variables sensibles si l'option est proposée.
Si une variable obligatoire manque, l'interface affiche une page « Configuration incomplète » qui en donne les **noms**
(jamais les valeurs) : complétez les variables, puis redéployez.

## 6. Importer le dépôt dans Vercel

1. Vercel : *Add New*, *Project*, puis *Import* du dépôt GitHub.
2. Ne modifiez pas les réglages de build : le fichier `vercel.json` du dépôt indique à Vercel quelle fonction Python
   utiliser et quels fichiers inclure. Il n'y a pas d'étape de compilation.
3. *Deploy*. Relancez un déploiement (*Redeploy*) après chaque changement de variable.

Le workflow `analyse.yml` doit exister sur la branche indiquée par `MLM_GH_REF` (par défaut `main`) : sans lui, le
lancement d'une analyse échoue avec un message « HTTP 404 ».

## 7. Premier test

1. **Accueil** : ouvrez l'adresse du projet (`https://<projet>.vercel.app`). Le navigateur demande l'identifiant et le
   mot de passe : l'identifiant peut être quelconque, le mot de passe est `MLM_PASSWORD`. L'accueil s'affiche, vide
   tant qu'aucune analyse n'a été publiée.
2. **Lancer une analyse test** : dans *Lancer une analyse*, saisissez un petit département, par exemple `48`
   (Lozère), sans cocher « Reprendre », puis *Lancer*. La page indique « Analyse en cours ».
3. **Suivre** : cliquez sur *Actualiser* (ou cochez « Actualiser automatiquement »). Le journal se met à jour
   toutes les minutes environ. Dans l'onglet *Actions* du dépôt GitHub, vous voyez aussi le run `analyse`.
4. **Résultat** : à la fin, le département apparaît dans le tableau de l'accueil, avec son compte rendu et son CSV.
5. **Annuler** (facultatif) : pendant une analyse, *Arrêter l'analyse*. L'annulation prend quelques instants ; l'analyse
   peut ensuite être reprise en cochant « Reprendre l'analyse précédente ».

En cas de problème, consultez les *Logs* du déploiement Vercel (*Deployments*, *Functions* ou *Runtime Logs*) et
l'onglet *Actions* de GitHub. Les journaux n'affichent jamais les valeurs des secrets.

## 8. Points d'attention

- **Quota GitHub Actions** : si le dépôt est **privé**, les minutes d'exécution comptent dans le quota mensuel de votre
  offre GitHub. Une analyse de toute la France est longue (plusieurs heures) : analysez par départements, ou rendez le
  dépôt public si cela vous convient (décision à prendre en connaissance de cause).
- **Adresses IP du cloud** : Vercel et GitHub Actions utilisent des adresses d'hébergeurs cloud. Certains sites de
  mairies les bloquent ou les limitent. Un département test permet de le mesurer : si le taux de sites inaccessibles
  est anormalement élevé, signalez-le avant de conclure à une absence de liens.
- **Durée des requêtes Vercel** : l'interface ne fait que de courtes lectures et des appels à l'API GitHub ; l'analyse
  elle-même ne tourne pas sur Vercel.
- **Renouvellement** : le jeton GitHub expire à la date choisie à l'étape 3 ; renouvelez-le et mettez à jour
  `MLM_GH_TOKEN` dans Vercel, puis redéployez.
- **Changer le mot de passe** : modifiez `MLM_PASSWORD` dans Vercel, puis redéployez.

## Ouvrir la lecture publique (racine sans mot de passe)

Facultatif. Avec cette option, l'adresse racine du site affiche les résultats publiés, sans mot de passe. Aucune analyse
ne peut être lancée, arrêtée ou modifiée depuis les pages publiques.

1. Vercel : *Settings*, *Environment Variables*, ajoutez `MLM_LECTURE_PUBLIQUE` avec la valeur `1` (environnement
   *Production*).
2. Relancez un déploiement (*Redeploy*) : la variable n'est lue qu'au démarrage.
3. Adresse à diffuser : `https://<projet>.vercel.app/`.

### Routes publiques et protégées

Routes publiques (lecture seule, GET et HEAD uniquement, `noindex`) :

| Adresse | Contenu |
|---|---|
| `/` | Liste des départements publiés, recherche, tableau « Tous les liens trouvés » (une ligne par commune) |
| `/a-propos` | Page « À propos » |
| `/resultats.csv` | CSV des communes à lien (une ligne par commune), filtres `q` et `dep` |
| `/departement/<code>`, `/departement/<code>.csv` | Vue assainie d'un département et son CSV |
| `/rapports` | Liste des rapports par département (dernière analyse de chacun) |
| `/rapports/<code>` | Rapport d'un département (vue assainie, identique pour tous) |
| `/rapports/<code>/communes.csv` | CSV des communes du département |
| `/rapports/<code>/export.html` | Export HTML autonome, assaini (même contenu que la vue publique) |
| `/rapport/<code>` | Ancienne adresse : redirigée (303) vers `/rapports/<code>` |
| `/campagnes` | Historique : analyses **terminées** uniquement, sans journal ni identifiant d'exécution |
| `/campagnes/<n>` | Détail d'une analyse terminée : dates, périmètre, nombre de communes, totaux par statut |
| `/static/...` | Ressources graphiques |

Une analyse en file, en cours, annulée ou en échec n'apparaît pas dans `/campagnes` et répond 404 sur `/campagnes/<n>`.
Les totaux par statut d'une campagne sont calculés sur les départements dont la dernière publication tombe dans la
période de l'analyse (pas de photographie par campagne en base).

Routes protégées par le mot de passe (toute autre adresse l'est par défaut) :

| Adresse | Contenu |
|---|---|
| `/tableau-de-bord` | Tableau de bord d'administration (chiffres, « Lancer une analyse ») |
| `/analyse`, `/suivi`, `/api/etat`, `/arreter`, `/reprendre/<n>`, `/lancer` | Lancement, suivi, arrêt et reprise des analyses |
| `/resultats` | Liste paginée des communes à lien, administrateur |
| `/admin/campagnes`, `/admin/campagnes/<n>` | Historique complet (toutes les analyses) et détail avec journal |
| `/admin/rapports/<code>` | Vue d'administration complète d'un département (détails techniques, liens écartés) |
| `/csv/<code>`, `/csv/tous` | Anciens CSV par département et global |

Le menu de l'administrateur, sur les pages publiques, ajoute « Campagnes (administration) » (`/admin/campagnes`). Sur les
pages assainies (`/rapports`, `/campagnes`), il ne comporte aucun lien d'exploitation. Sans `MLM_LECTURE_PUBLIQUE`, les
adresses `/rapports` et `/campagnes` restent protégées et montrent la vue d'administration complète, comme avant.

Sur la racine, sous le tableau des départements, le tableau « Tous les liens trouvés » liste **une ligne par commune**
(département, code INSEE, nom de la commune, page de la commune où figure le lien, lien vers Vos questions, date « Apparu
le »). Pour une commune, un seul lien est retenu, de façon déterministe : 1° une cible propre à la commune (son chemin
contient le code INSEE ou le slug) avant une cible générique (racine du site, avec ou sans `?utm_…`) ; 2° pour la page
source, https avant http, sans paramètres de suivi (`utm_*`, `fbclid`, `gclid`), puis la plus courte, puis ordre
alphabétique ; 3° à égalité, la plus ancienne première vue. Les adresses sont normalisées pour comparer (schéma, hôte en
minuscules sans `www.` ni point final, sans fragment, sans `/` final, port par défaut retiré, paramètres de suivi
retirés). « Apparu le » est la plus ancienne première vue de tous les liens de la commune.

La pagination compte des communes (100 par page). Le compteur indique « N communes ». Le lien « Télécharger tout (CSV) »
(`/resultats.csv`, mêmes filtres `q` et `dep`) exporte une ligne par commune, aux colonnes `departement_code`,
`departement`, `code_insee`, `commune`, `page_source`, `url_cible`, `apparu_le` ; il est plafonné à 200 000 communes. Seuls
les liens confirmés sont listés, jamais les cas « à vérifier » ni les erreurs.

**Nouveaux liens.** Au-dessus du tableau complet, la section « Nouveaux liens » liste les communes ayant un lien
confirmé apparu depuis moins de 30 jours (constante `FENETRE_NOUVEAUX_JOURS`), une ligne par commune, de la plus récente à
la plus ancienne, 50 au plus. Seuls les liens nouveaux comptent pour le lien retenu et pour « Apparu le ». Le tableau
complet affiche la date « Apparu le » et le repère « nouveau » (date dans la fenêtre) ; le CSV porte la colonne
`apparu_le`. Comment cela fonctionne :

- Postgres : la table `liens_vus` garde, pour chaque lien (département, code INSEE, page, cible), la date de sa première
  apparition. Elle est alimentée par la publication (`publish`) : un lien déjà connu n'est jamais réinséré, un lien
  disparu puis réapparu garde sa première date, rien n'est supprimé. Republier des résultats identiques ne crée aucun
  nouveau lien.
- Amorçage : à la création de la table (première mise à jour de la base après cette version), les liens déjà publiés
  sont enregistrés comme non nouveaux, datés de leur publication. Seuls les liens apparus ensuite sont « nouveaux ».
  Un département publié pour la première fois après cette mise à jour voit tous ses liens marqués nouveaux.
- Local (FileSource) : la table `liens_vus` est dans `data/historique.sqlite`. Faute de point d'accroche dans le
  moteur de crawl, elle est mise à jour à chaque lecture ; la date d'un nouveau lien est donc celle de la première
  lecture après le contrôle. Les pages publiques en local écrivent donc dans cette base.

Accès à l'administration : la page publique propose un lien « Espace réservé » vers `/analyse`, qui demande le mot de
passe. Le navigateur qui a fourni le bon mot de passe voit ensuite, sur la racine et sur les pages publiques, les mêmes
résultats que les visiteurs, avec le menu complet (« Tableau de bord », « Analyse », « Suivi », « Rapports »,
« Résultats », « Historique »). Un identifiant invalide sur une page publique est simplement traité comme un visiteur.

Le tableau de bord d'administration (dernière analyse, chiffres, bouton « Lancer une analyse ») n'est plus à la racine
quand la lecture publique est ouverte : il est sur `/tableau-de-bord`, toujours protégé par le mot de passe. Il reste
accessible par l'entrée « Tableau de bord » du menu. Sans `MLM_LECTURE_PUBLIQUE`, la racine reste le tableau de bord
protégé, comme avant. Aucun bouton de lancement n'apparaît dans la vue résultats, même pour l'administrateur.

Les pages publiques sont marquées `noindex`, et la réponse varie selon l'identifiant (`Vary: Authorization`, sans cache).

Pour couper : supprimez `MLM_LECTURE_PUBLIQUE` (ou donnez-lui une valeur autre que `1`, `true` ou `oui`), puis relancez
un déploiement. La racine redemande alors le mot de passe, comme avant.

## Adresse principale et ancienne adresse (redirection)

Le même déploiement répond sur deux noms d'hôte : l'adresse principale `mairie-link-monitor.vercel.app`, et
`mairie-link-monitor-neutralex.vercel.app`, alias automatique de Vercel qui ne peut pas être supprimé. Pour que toute
visite passe par l'adresse principale, renseignez dans Vercel (*Settings*, *Environment Variables*, environnement
*Production*) :

| Nom | Valeur | Obligatoire |
|---|---|---|
| `MLM_HOTE_PRINCIPAL` | `mairie-link-monitor.vercel.app` (nom d'hôte seul, sans `https://` ni chemin) | non |
| `MLM_HOTES_REDIRIGES` | `mairie-link-monitor-neutralex.vercel.app` (plusieurs noms séparés par des virgules) | non |

Puis relancez un déploiement (*Redeploy*) : les variables ne sont lues qu'au démarrage.

Effet : une requête adressée à un nom listé dans `MLM_HOTES_REDIRIGES` reçoit une réponse `308 Permanent Redirect` vers
`https://mairie-link-monitor.vercel.app`, avec le même chemin et la même requête (la méthode est conservée). La
redirection a lieu avant toute demande de mot de passe. Les adresses d'aperçu (déploiements de branche) et l'adresse
principale ne sont pas touchées. Les noms sont normalisés (minuscules, sans port) ; une valeur invalide est ignorée et
un avertissement est journalisé, sans interrompre le site. Si une seule des deux variables est renseignée, rien n'est
redirigé.

Pour couper la redirection : supprimez `MLM_HOTES_REDIRIGES` (ou laissez-la vide), puis relancez un déploiement. Les deux
noms répondront alors à l'identique.

## Durcissement recommandé

- **Environnement GitHub** (facultatif) : dans *Settings → Environments*, créez un environnement limité à la branche
  `main`, puis placez-y le secret `DATABASE_URL`. Ajoutez ensuite `environment: <nom>` au job `analyse` si vous le
  souhaitez : le secret ne sera alors accessible qu'aux exécutions sur `main`.
- **Actions épinglées par SHA** : remplacez les références `@v4` des actions (`actions/checkout`, `actions/setup-python`,
  `actions/cache`) par le SHA complet du commit voulu, pour qu'une mise à jour amont ne modifie pas le workflow à votre insu.
- **Départements retirés** : un département retiré de l'inventaire reste visible dans l'interface tant qu'on ne le
  supprime pas de la base. Les publications sont cumulatives, c'est un choix assumé : une analyse ne retire jamais
  de résultat déjà publié.
