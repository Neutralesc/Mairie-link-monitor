# mairie-link-monitor (v0.16)

Détecte, pour un ou plusieurs départements français, les sites de mairies qui contiennent un lien vers
https://vosquestions.ecologie-territoires.gouv.fr, et exporte **(code INSEE, URL de la page où le lien est trouvé)**.

## Application web (pour les collègues)
Double-cliquez sur **`lancer_web.bat`** (ou `py -3 src\mairie_link_monitor.py web`), puis ouvrez **http://localhost:8080**.
La page permet de lancer une analyse (département(s), reprise), de suivre son avancement (mise à jour automatique), de
l'arrêter, et d'ouvrir pour chaque département le **compte rendu** et le **CSV**. Une seule analyse à la fois.

Pour la rendre accessible aux collègues du réseau, dans `config.toml` :
```toml
adresse_web = "0.0.0.0"
port_web = 8080
mot_de_passe_web = "un mot de passe à communiquer aux collègues"
```
Ils ouvrent alors `http://<nom-du-poste>:8080` et saisissent le mot de passe (n'importe quel identifiant). Sans mot de
passe, l'application refuse d'écouter sur le réseau. Le pare-feu Windows demandera l'autorisation au premier lancement
(réseau privé uniquement). Le poste doit rester allumé pendant les analyses.
Ne pas exposer l'application sur Internet telle quelle : le mot de passe circule sans chiffrement (HTTP). Pour un accès
hors du réseau local, passer par le VPN de la structure ou un serveur web avec HTTPS devant l'application.

## Historique et compte rendu (lots 4 et 5)
À la fin de chaque analyse, l'état de chaque commune est enregistré dans `data\historique.sqlite` et comparé au contrôle
précédent : **nouveaux liens**, **liens disparus confirmés** (site ou page PanneauPocket lu sans y retrouver le lien),
**liens non retrouvés à vérifier** (site illisible ou lu partiellement : pas une preuve de disparition), **liens déplacés**.
Le compte rendu `resultats\rapport_<dep>.html` (aussi dans l'application web) présente les chiffres clés, ces changements,
la liste des communes avec lien et les communes à vérifier.
```powershell
py -3 src\mairie_link_monitor.py historique --dept 60   # changements depuis le contrôle précédent
py -3 src\mairie_link_monitor.py rapport --dept 60      # régénère le compte rendu
```

## Rappel maximal (v0.16)
Ajouts issus de l'analyse des résultats du 28/10 (liens manqués, 217 sites « lus » en une seule page) :
- **Liens non balisés** : si aucun `<a href>` ne mène au portail, le code de la page est cherché (onclick, `data-*`, lien de
  redirection `…?url=https%3A%2F%2Fvosquestions…`, widget dans un `<script>`) : types `lien_attribut` / `lien_script`, comptés
  et exportés. Une URL écrite en **texte brut sans lien** n'est pas comptée : elle est signalée dans `motif`.
- **Plan B si l'accueil n'a aucun autre lien interne** (page JavaScript, protection anti-robot, site d'une page) : lecture du
  plan du site (`sitemap.xml`) puis de quelques pages usuelles (`/plan-du-site`, `/mentions-legales`, `/contact`…), dans le
  respect de `robots.txt`. La cause probable est écrite dans la colonne **`accueil_diag`** de `crawl_sites.csv`
  (« page construite par JavaScript probable », « protection anti-robot probable »…) et le site passe en `explore_partiel`
  au lieu de « lu en entier ». Au premier `--resume`, les petits sites « complets » lus avant cette version sont refaits une fois.
- **Pièges d'URL** (rapide ET plus de pages utiles) : les paramètres de suivi (`utm_*`, `fbclid`…) sont retirés des adresses
  (même page = une seule lecture) ; au plus 8 pages par gabarit de pagination ou de calendrier (`?page=`, `?date=`,
  `/page/N/`, `/2024/05/`). Les pages de contenu ne sont jamais plafonnées. Colonne `variantes_ignorees`.
- **robots.txt** en redirection sans destination ou en boucle : traité comme absent (RFC 9309) au lieu de bloquer le site.
- **TLS ancien** : pour les sites à certificat invalide, le client accepte aussi les vieux serveurs (TLS 1.0/1.1, clés faibles).
- **Mesure du débit corrigée** : `octets_recus` compte désormais les octets reçus sur le réseau (compressés) et non plus les
  octets après décompression, qui surestimaient fortement le débit des mesures précédentes.
- **Durée et pages** : avec 1,3 s de politesse, 180 s permettent au plus ≈ 138 pages par site, quelle que soit la limite de
  700 pages. Le programme l'indique en fin d'exploration quand plus de 10 % des sites sont coupés par la durée.

## Installation (PowerShell, dans le dossier du projet)
```powershell
py -3 -m pip install -r requirements.txt
py -3 -m unittest discover -s tests
```

## Lancement
Double-cliquez sur **`lancer.bat`** (ou `py -3 src\mairie_link_monitor.py` sans argument). Le programme **demande les
départements** (ex : `60`, ou `60 80 2A`, ou `tous`), l'e-mail de contact (une fois, mémorisé), propose de reprendre un run
déjà commencé, affiche un récapitulatif et attend votre confirmation. Le dernier choix est proposé par défaut.

Tous les réglages sont dans **`config.toml`** (créé au premier lancement, valeurs commentées ; un ancien fichier est migré
automatiquement et sauvegardé en `.bak`). Rien d'autre à taper.

Ligne de commande facultative (prioritaire sur config.toml) :
```powershell
py -3 src\mairie_link_monitor.py run --dept 60 80 02         # sans question
py -3 src\mairie_link_monitor.py run --dept 60 --resume      # reprise
py -3 src\mairie_link_monitor.py export --dept 60            # régénère le CSV final sans réseau
py -3 src\mairie_link_monitor.py coverage --dept 60          # combien de communes sont analysées, et pourquoi pas les autres
py -3 src\mairie_link_monitor.py compare --dept 60 --ancien "C:\...\links_found_oise.csv"   # explique un écart avec d'anciens résultats
py -3 src\mairie_link_monitor.py migrate-legacy --dept 60 --source "C:\...\ancien-dossier"
```
`--dept all --confirm-all` = tous les départements (très long). `1` est interprété comme `01`.

## Ce que fait le programme
Pour chaque département : **inventaire** (COG INSEE + API DILA) → **découverte** (sites des communes sans site référencé)
→ **exploration** → **export**. Les départements demandés sont explorés dans un seul pool, **et les résultats d'un
département sont écrits dès que tous ses sites sont traités** (`resultats\liens_<dep>.csv`, cumul dans
`liens_tous_departements.csv`) ; des points de sauvegarde sont écrits toutes les 100 communes, et `Ctrl+C` écrit les
résultats partiels (reprise avec `--resume`).

- **Exploration** : parcours **en largeur, sans aucune priorité de contenu** ; **arrêt au premier lien trouvé** sur un site.
- **Analyse HTML** : selectolax (moteur Lexbor) à la place de BeautifulSoup.
- **Couverture maximale** : tous les sites candidats sont tentés (la vérification préalable n'est plus un filtre ; elle est
  supprimée du pipeline par défaut, ce qui divise le nombre de requêtes) ; si l'accueil échoue : racine du site, avec/sans
  `www`, http/https ; certificat invalide : exploré sans vérification et signalé (`ssl_invalide`) ; échec passager (timeout,
  connexion, 5xx) : une seconde tentative, plus lente.
- **Sites déménagés** : si l'accueil redirige vers un autre domaine (ex. `beauvais.fr` → `beauvaisis.fr`), le site est exploré
  à sa nouvelle adresse (colonne `erreur` : « site déplacé »). `robots.txt` qui redirige vers un autre domaine est suivi (RFC 9309) ;
  un `robots.txt` en 5xx ou 429 est retenté une fois.
- **Reprise (`--resume`)** : ne refait que les sites en échec **et les sites « explore_partiel » dont les limites ont été
  relevées** (ex. `pages_max_par_site` passé de 40 à 150). Pour augmenter la couverture : modifier `config.toml`, puis relancer en reprise.
- **Découverte** : pour une commune sans site DILA, noms de domaine usuels (`nom.fr`, `mairie-nom.fr`, `nom60.fr`…) ; retenu
  seulement si la page d'accueil (robots.txt respecté) contient le nom de la commune, un terme mairie/commune et un code
  postal du département ; une redirection vers un autre domaine (site hébergé ailleurs) est suivie puis validée sur son contenu.
  Résultat marqué `devine_a_verifier`, jamais présenté comme officiel. Pour retester les communes déjà testées : relancer
  `--steps discover` **sans** `--resume`.

## PanneauPocket (étape `plateformes`, après l'exploration des sites)
Beaucoup de communes sans site publient leurs informations sur PanneauPocket : une page par commune
(`https://app.panneaupocket.com/ville/<numéro>-<nom>-<code postal>`) qui affiche les annonces en cours, liens compris.
Pages lues : celles déclarées par les mairies dans la DILA (champ `application_mobile`), celles liées depuis les sites
des mairies, celles de `pages_panneaupocket` dans config.toml, celles du passage précédent, puis **les communes voisines
du même département** affichées sur chaque page. Une page est rattachée à une commune par la DILA, sinon par son nom et
son code postal ; les pages qui ne sont pas des mairies (gendarmerie, intercommunalité) servent à trouver d'autres
communes mais ne sont jamais rattachées. robots.txt et le délai par site sont respectés : une seule requête à la fois
vers PanneauPocket (compter environ `delai_par_site_s` secondes par commune lue).
Résultats : `reports\<dep>\plateformes.csv` (pages lues, rattachement, statut) et `liens_plateformes.csv` ; les liens
s'ajoutent au CSV final (le site de la mairie reste prioritaire en mode une page par commune) et à `couverture.csv`
(« lien trouvé (PanneauPocket) », « page PanneauPocket lue, aucun lien »).
Pas encore pris en charge : Illiwap (une page par article), IntraMuros (pages construites en JavaScript).

## Résolution DNS (v0.14)
Avec beaucoup de requêtes simultanées, le serveur DNS de la box sature : le 28/10, 111 sites de l'Oise lus sans problème
le 25/10 sont sortis en `erreur_dns`. Chaque site est désormais résolu avant d'être exploré, avec au plus
`resolutions_dns_simultanees` résolutions en même temps (48 par défaut, tous processus) et 3 essais espacés ; une erreur
DNS est retentée une seconde fois en fin de passe, et la colonne `erreur` garde le code Windows (11001 = nom inexistant,
11002 = échec temporaire). Pour refaire les sites en erreur DNS d'un passage précédent : relancer en reprise.
Les pages d'accueil qui redirigent (`<meta http-equiv="refresh">`, cadres `<frame>`/`<iframe>`, `location.href = …`) sont
désormais suivies.

## Performance (asyncio + processus) et règles de politesse
- **asyncio** : une file de sites traitée par des tâches concurrentes ; **plusieurs processus** (un par cœur utile, laissant un
  cœur au processus principal), sites répartis par domaine d'hébergement ; plafond de **100 tâches par processus** (au-delà,
  le débit baisse : mesuré). Réglages : `parallelisme`, `processus` (0 = automatique), `parallelisme_max_par_processus`.
- La résolution DNS (threads) est relevée pour ne pas brider le parallélisme ; connexions réutilisées par processus.
- **Règles d'exploration respectées** : `robots.txt` lu pour chaque origine avant toute page (Disallow, `Crawl-delay`) ;
  `robots.txt` en erreur 5xx = pas d'exploration, 401/403 = tout interdit ; **1 requête par seconde maximum par site**
  (`delai_par_site_s`) ; **4 sites simultanés maximum par domaine d'hébergement** (`max_sites_par_domaine`) ;
  ralentissement automatique d'un site qui répond 429/503 ; User-Agent identifiable avec votre contact ; pages limitées en
  nombre (40), profondeur (2), taille (5 Mo) et durée (180 s par site) ; pas d'exécution de JavaScript.
- Mesure faite pendant le développement (serveur simulé en local, 1 seul cœur, latence 50 ms, 300 sites × 6 pages) :
  24 tâches → 103 pages/s ; 96 tâches → 187 pages/s ; 300 tâches dans un seul processus → 92 pages/s (d'où le plafond). Le
  coût CPU observé est d'environ 4 ms par page et par cœur. Ce n'est PAS une mesure sur votre machine ni sur Internet.

## Régler le parallélisme sur des mesures
Chaque exploration enregistre, toutes les 10 s et par processus, dans `data\performance\perf-<date>.csv` (+ `.json` : machine et
réglages) : requêtes/s, **sites en cours** (occupation du pool) et **file d'attente**, requêtes en vol et en attente de politesse,
issues (réussites, 4xx, 5xx, 429/503, timeouts, erreurs de connexion/DNS/SSL), **latence réseau** p50/p95 (hors attente de politesse),
**octets reçus et débit (Mo/s)**, **CPU du processus** et **retard de la boucle asyncio** (saturation locale).
`py -3 src\mairie_link_monitor.py tune` (ou la fin d'une exploration) analyse le dernier fichier en séparant :
- la **phase pleine** (la file contient encore des sites) : c'est là que le parallélisme agit ; les taux d'échec, le CPU et la
  saturation du pool sont calculés sur cette phase ;
- la **traîne** (file vide, on attend quelques sites lents) : plus de parallélisme n'y change rien. Leviers : plusieurs départements
  ensemble, `duree_max_par_site_s` plus court en première passe puis reprise avec une valeur plus haute (les sites coupés par la durée
  sont refaits si elle est augmentée), et les sites les plus lents du passage précédent démarrent en premier.
Lecture : CPU élevé ou retard de boucle > 200 ms = processeur saturé ; timeouts/429/503 > 5 % = trop de requêtes pour la connexion ou
les sites ; pool occupé à moins de 85 % en phase pleine = limite due à la politesse (1 req/s par site), pas au parallélisme.
Pour comparer : même petit département, plusieurs valeurs de `parallelisme`, puis comparer `requetes_par_s_phase_pleine` et
`taux_echec_passager`. Désactivable : `enregistrer_mesures_performance = false`.

## Résultats et formats
`resultats\liens_<dep>.csv` : `code_insee` et `url_page`, **une page par commune**. Séparateur `;` par défaut (Excel
français ; avec `,` Excel ne séparait pas les colonnes). Pour garder le zéro initial des codes (01…), importer la colonne en « Texte ».
`reports\<dep>\` : `links_found.csv` (zone `menu_ou_bandeau`/`contenu`, `cible_commune` : `oui`/`autre_commune`/`generique`,
confiance du site), `crawl_sites.csv` (statut par site, pages lues, tentatives, processus), `couverture.csv` (état de chacune
des communes du COG), `sites_devines.csv` (colonne `rejets` : pourquoi chaque domaine testé a été écarté), `crawl_summary.json`.
Statuts d'un site : `lien_trouve`, `explore_complet`, `explore_partiel`, `erreur_acces`, `robots_interdit`,
`robots_indisponible`, `non_explore`. **« Aucun lien » n'est fiable que pour `explore_complet`.**

## Limites
- Validé sur données réelles pour l'**Oise (60)** uniquement (arrondissements, communes nouvelles, Corse, outre-mer à vérifier).
- Sites en JavaScript (liens chargés côté client) : non vus. Liens via raccourcisseur/redirection : non suivis.
- La découverte et la classification de confiance sont des heuristiques.
- Le débit réel dépend de votre connexion et de votre processeur ; réduisez `parallelisme` si l'avertissement
  « n'ont pas pu être lus » apparaît.
- Retiré par rapport aux versions précédentes : relevé complet des pages (étape `locate`), profil « complet », priorisation par
  mots-clés, détection de l'URL en texte. L'étape `verify` (diagnostic) reste disponible avec `--steps verify` mais n'est plus nécessaire.
