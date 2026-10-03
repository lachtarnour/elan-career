# LinkedIn via Apify

Source id : `linkedin`

LinkedIn est interrogé via l'actor Apify `valig/linkedin-jobs-scraper`. La
source sert surtout à augmenter le rappel sur les offres récentes publiées sur
LinkedIn, tout en gardant les champs bruts dans `source_data` pour audit.

## Stratégie rapide

| Phase | Stratégie |
| --- | --- |
| Collecte | Runs Apify asynchrones suivis par identifiant, avec titre, localisation, fraîcheur, contrat, niveau et modalité. |
| Filtre local | Utilise contrat, lieu, remote/hybrid et niveau d'expérience LinkedIn. |
| Analyse LLM | Envoie un bloc compact : URL LinkedIn/apply, recruteur si présent, entreprise et faits structurés. |
| Limite API | La valeur demandée s'applique à chaque requête LinkedIn, avec `LINKEDIN_MAX_RESULTS` comme maximum configurable (jusqu'à `300`). Aucun plafond global sur le cumul des résultats. |
| Limite produit | Les offres `EASY_APPLY` peuvent ne pas exposer d'URL externe de candidature. |

## Configuration

| Variable | Rôle |
| --- | --- |
| `APIFY_TOKEN` | Token Apify. |
| `LINKEDIN_CONTRACT_TYPE` | Codes actor, par défaut `F` pour full-time. |
| `LINKEDIN_EXPERIENCE_LEVEL` | Codes actor, par défaut `2,3,4`; `4` est utilisé seulement en fallback après `2,3`. |
| `LINKEDIN_REMOTE` | Codes actor, par défaut `1,2,3`. |
| `LINKEDIN_DATE_POSTED` | Fraîcheur par défaut : `any`, `today`, `3days`, `week`, `month` ou token Apify `r86400`. `3days` est élargi à `r604800`, car l'actor ne déclare pas de filtre 3 jours. |
| `LINKEDIN_HTTP_TIMEOUT` | Délai réseau de chaque appel API court, par défaut `30` secondes. Ce n'est pas une limite de durée du run. |
| `LINKEDIN_POLL_WAIT_SECONDS` | Attente longue côté API lors de chaque lecture du statut, par défaut `5` secondes. |
| `LINKEDIN_READ_MAX_ATTEMPTS` | Nombre maximal d'échecs réseau consécutifs pendant la lecture d'un run ou dataset connu, par défaut `5`. |
| `LINKEDIN_READ_FAILURE_BUDGET_SECONDS` | Durée maximale d'une même série d'échecs réseau, par défaut `180` secondes. |
| `LINKEDIN_MAX_CONCURRENT_RUNS` | Taille configurée des cohortes. Les anciennes valeurs `4` ou `5` restent acceptées, mais le plafond effectif est toujours `3` runs Actor. |
| `LINKEDIN_MAX_RESULTS` | Limite par requête LinkedIn/Apify ; défaut `50`, maximum autorisé `300`. |

## Appels

| Appel | Auth | Usage |
| --- | --- | --- |
| `POST api.apify.com/v2/actors/valig~linkedin-jobs-scraper/runs` | En-tête `Authorization: Bearer …` | Lance l'actor et renvoie immédiatement son identifiant. |
| `GET api.apify.com/v2/actor-runs/{runId}` | En-tête `Authorization: Bearer …` | Suit le même run jusqu'à un état terminal. |
| `GET api.apify.com/v2/datasets/{datasetId}/items` | En-tête `Authorization: Bearer …` | Récupère les offres du dataset du run. |
| `POST api.apify.com/v2/actor-runs/{runId}/abort` | En-tête `Authorization: Bearer …` | Annule un run connu lorsque la recherche est interrompue. |

Paramètres importants :

| Paramètre | Origine Élan |
| --- | --- |
| `title` | Requête rôle. |
| `location` | Localisation demandée. |
| `datePosted` | Conversion de `date_posted`, ex. `today -> r86400`. |
| `contractType` | `LINKEDIN_CONTRACT_TYPE` ou override. |
| `experienceLevel` | `LINKEDIN_EXPERIENCE_LEVEL` ou override. |
| `remote` | `LINKEDIN_REMOTE` ou override. |
| `limit` | `max_results` pour chaque titre recherché, ou le complément lors du fallback d'expérience ; aucun plafond sur le cumul des titres. |

Le champ « Résultats par source » de l'application est une limite **par requête**
pour LinkedIn/Apify. Toutes les requêtes du plan sont parcourues, y compris les
variantes de titres et les recherches doctorales. Par exemple, 21 requêtes de
100 résultats peuvent fournir 2 100 offres avant déduplication et filtrage local.
Chaque réponse reçue est traitée intégralement, même si l'actor dépasse la limite
demandée. Une offre déjà connue est ignorée localement, mais elle faisait bien
partie des `limit` résultats bruts de cette requête : Élan ne rallonge pas cette
requête pour la remplacer. Cela ne réduit ni le nombre ni la limite des autres
requêtes du plan.

Les runs Actor sont lancés par cohortes fixes de trois au maximum. Élan attend la
fin des trois runs avant d'en soumettre d'autres. Un éventuel fallback de niveau
d'expérience forme lui aussi une nouvelle cohorte : un run lent retarde donc la
cohorte suivante. Une erreur fatale (authentification, quota, démarrage incertain
ou perte durable du suivi d'un run connu) arrête les cohortes suivantes, sans
interrompre les runs déjà soumis. Une erreur limitée à une requête conserve ses
résultats partiels, ajoute un avertissement et laisse les cohortes suivantes
continuer. Les résultats sont fusionnés dans l'ordre initial du plan afin de
garder une déduplication déterministe.

Il n'existe plus de timeout client global pour un run. Élan conserve le `runId`,
attend son état terminal, puis lit son dataset. Les lectures de statut et de
dataset d'un run connu sont sûres à répéter et s'arrêtent après cinq échecs
consécutifs ou trois minutes ; une lecture de statut réussie remet ces compteurs
à zéro. L'échéance est vérifiée avant chaque nouvelle lecture et son timeout
réseau est réduit au budget restant. Le timeout Requests concerne les échanges
réseau ; il ne constitue pas une garantie de durée totale d'un flux HTTP qui
continue à envoyer des données. Le `runId`, le `datasetId`, la requête et le
payload sont enregistrés
atomiquement dans le journal local `apify_pending_runs.json`. Le prochain passage
reprend ce même run ou dataset au lieu d'envoyer un second POST. Si une passe
fallback reste en attente après le commit de la passe primaire, elle est reprise
directement : le primaire n'est pas relancé. La limite et les identifiants à
ignorer ne changent pas l'identité de cette passe ; son payload initial reste
celui de la récupération, même si la limite demandée a changé.

Lorsqu'un plan contient des runs connus en attente, ce passage est réservé à
leur récupération, sans lancer les autres requêtes ni de nouveaux Actors.
Un avertissement explique ce comportement. Une recherche suivante peut repartir
après leur enregistrement local. Un changement de titre, lieu, fraîcheur ou
filtres identifie une recherche distincte et ne récupère pas automatiquement les
runs des anciens paramètres. Ces derniers restent dans le journal.

Le message terminal et le caractère fatal d'un échec sont conservés avec le
dataset : un crash ne transforme pas une erreur de quota en erreur récupérable.
Les journaux de version 1 restent lisibles ; si une ancienne entrée terminale
en échec ne contient pas sa cause, Élan relit les métadonnées du run et arrête
la récupération si cette cause ne peut pas être obtenue.

L'entrée n'est supprimée qu'après l'enregistrement local des offres ; un crash entre le commit
SQLite et ce nettoyage provoque seulement une relecture idempotente du dataset.
Le journal utilise aussi un verrou de processus et inscrit une réservation avant
chaque POST : deux recherches identiques lancées simultanément ne peuvent donc
pas créer deux runs. Si la connexion casse pendant le POST avant que l'identifiant
soit connu, cette réservation reste volontairement bloquante jusqu'à vérification
manuelle dans Apify ; l'application ne devine jamais qu'il est sûr de repayer un
run.

Le POST de démarrage transmet aussi `maxItems` afin de borner le nombre d'items
facturables pour les actors pay-per-result, et n'est jamais retenté si sa réponse
est incertaine. Élan n'envoie `abort` qu'après une annulation explicite de
l'utilisateur. Une erreur réseau, d'authentification ou de quota n'annule donc
jamais automatiquement un Actor qui peut encore continuer côté Apify.

Codes OpenAPI utiles :

| Champ | Codes envoyés |
| --- | --- |
| `contractType` | `F=Full-time`, `P=Part-time`, `C=Contract`, `T=Temporary`, `I=Internship`, `O=Other`. |
| `experienceLevel` | `1=Internship`, `2=Entry level`, `3=Associate`, `4=Mid-Senior level`, `5=Director`, `6=Executive`. |
| `remote` | `1=On-site`, `2=Remote`, `3=Hybrid`. |

Stratégie expérience : Élan appelle d'abord LinkedIn avec
`experienceLevel=["2", "3"]`. Si le nombre d'offres retournées est inférieur à
`max_results`, il lance un second appel avec `experienceLevel=["4"]` et
`skipJobId` pour éviter de revoir les offres déjà collectées.

## Mapping

| Sortie | Champs LinkedIn/Apify |
| --- | --- |
| `RawJob.title` | `title`. |
| `RawJob.company` | `companyName`. |
| `RawJob.location` | `location`. |
| `RawJob.contract_type` | `contractType`. |
| `RawJob.remote_policy` | `remote`, `workType` ou `location` si remote/hybrid est visible. |
| `RawJob.description` | `descriptionHtml` nettoyé, fallback `description`. |
| `RawJob.application_url` | `applyUrl`, fallback `url`. |
| `RawJob.published_date` | `postedDate`. |
| `source_data` | Payload Apify brut + `_smartapply_search`. |

## Stratégie filtre

Adapter : `build_linkedin_filter_facts`.

| Fact normalisé | Source | Effet |
| --- | --- | --- |
| `structured_contract_type` | `contractType` | Rejet stage, alternance, freelance, part-time, etc. si détecté. |
| `structured_location` | `location` | Rejet des lieux hors zone acceptée. |
| `structured_remote_policy` | `remote`, `workType`, `location` | Diagnostic remote/hybride. |
| `experience_requirement` | `experienceLevel` | Signal d'expérience pour audit et analyse locale. |

## Stratégie analyse

Builder : `build_linkedin_source_metadata`.

| Section | Contenu |
| --- | --- |
| `CONTACT_AND_APPLICATION_METADATA` | Source, entreprise, recruteur, type de candidature, URL offre/apply/company/recruiter. |
| `STRUCTURED_JOB_FACTS` | ID, titre, lieu, publication, candidatures, niveau, contrat, secteur, salaire et contexte de recherche. |
