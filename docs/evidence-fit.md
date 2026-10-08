# Évaluation de pertinence `evidence_fit_v6`

## Contrat envoyé et source figée

Les nouvelles extractions utilisent `OfferExtractionOutput`, qui conserve les
15 champs d'`OfferAnalysis` et ferme uniquement `seniority` et `offer_language`.
`OfferAnalysis` reste le lecteur des faits historiques, sans conversion de leurs
valeurs. L'unique deuxième appel utilise
`JobFitDecisionEvaluation` (`job_fit_evaluation_v2`), le prompt
`llm/prompts/templates/job_fit/system.j2` et le template `user.j2` de ce dossier.
Les versions du lecteur et du calcul sont `fit_decisions_reader_v2` et
`evidence_fit_calculation_v3`.

Les catégories du premier appel sont figées dans le schéma et le prompt :

- `seniority` : `junior`, `mid`, `senior`, `lead`, `manager`, `mixed`, `unknown`.
- `offer_language` : `fr`, `en`, `de`, `es`, `it`, `nl`, `pt`, `other`, `unknown`.

`mixed` désigne plusieurs niveaux explicitement visés. `other` désigne une langue
identifiable hors liste ; `unknown` conserve une incertitude sans deviner.
Les langues correspondent aux codes déjà reconnus par la détection locale.
Le contrat fermé s'applique à l'immédiat, au Batch et aux réextractions approuvées.
Son nom, son schéma et son prompt produisent une nouvelle identité de cache et de
checkpoint. Une réponse à une ancienne requête Batch garde sa clé historique :
elle ne devient pas une extraction du nouveau contrat lors de sa réception.
Les scores historiques, les faits réutilisés pour un fit seul et les documents
existants ne sont pas convertis. Aucun nouveau filtre de rejet n'est activé.

Pour les futures lettres, `other` et `unknown` restent enregistrés tels quels.
La langue de rédaction est résolue localement depuis le texte de l'annonce ;
sans détection confiante, le français est un choix documentaire explicite avec
une observation, pas une affirmation sur la langue de l'offre. Les étiquettes
HTML des lettres restent limitées au français et à l'anglais comme auparavant.

Le corps normalisé `offer_body` issu de la provenance figée est transmis une
seule fois avec les métadonnées déjà fournies à l'extraction. `fit_source` conserve
sa disponibilité explicite, sa référence `offer_source:body`, son texte et son
empreinte. La source est sauvegardée par offre, sans entrer dans le fingerprint
candidat global. En absence de source, le modèle dispose des faits et extraits ;
aucune réextraction implicite n'est demandée. Les protections d'association de
`reusable_offer` et `_source_matches` restent en place.

Le cache LLM garde sa purge à **15 jours**. Pour les nouvelles analyses terminées,
la preuve de l'extraction ne dépend pas de ce cache : `fit_diagnostics` conserve
les faits extraits, leur empreinte et l'`analyzer_input` utilisé. Le parcours
immédiat garde aussi la clé de requête et le modèle ; le Batch garde l'identifiant
du lot et son journal. `reusable_offer` peut relire ces données après la purge,
à condition que la source actuelle corresponde toujours à celle de l'extraction.
Cette garantie ne reconstitue pas la provenance absente des analyses historiques.

L'annonce peut expliciter une option, une alternative ou une condition que les
faits structurés ont mal restituée. Le modèle conserve la citation et son
interprétation dans `offer_interpretations`, sans corriger les champs partagés.
La validation de citation et la couverture sont uniquement consultatives.

## Quatre décisions nécessaires

Le nouveau minimum est **trois décisions d'axe plus `preference_conflict`**.
Un quatrième champ absent n'est remplacé ni par `none` ni par `unknown` ; aucune
nouvelle note n'est publiée, l'ancienne évaluation réussie reste intacte. Cette
règle remplace « trois niveaux suffisent » uniquement pour le nouveau contrat.

```json
{
  "decision": {
    "missions": "strong_fit",
    "essentials": "strong_fit",
    "responsibilities": "credible_limited_adaptation",
    "preference_conflict": "unknown"
  },
  "dimensions": {
    "missions": {"findings": []},
    "essentials": {"findings": []},
    "responsibilities": {"findings": []}
  },
  "issues": [],
  "neutral_offer_refs": [],
  "offer_interpretations": []
}
```

Ce JSON synthétique donne 95 %, avec observations sur les annotations absentes.
L'enveloppe est placée avant les annotations pour permettre la récupération
après une coupure. Le parseur de JSON partiel existant n'est pas modifié ; le
lecteur traite les doublons, les préfixes de clés échappées et les décisions
réellement absentes, hors enum ou contradictoires sans inventer de valeur.

| État | Niveau dérivé |
| --- | --- |
| `very_low_support` | 0 |
| `weak_support` | 1 |
| `partial_core_fit` | 2 |
| `credible_material_question` | 3, avec plafond 0,79 |
| `credible_limited_adaptation` | 3 |
| `strong_fit` | 4 |

Python calcule `(50 × missions + 30 × essentials + 20 × responsibilities) / 400`,
applique le minimum avec les plafonds (missions ≤ 1 : 0,39 ; un axe ≤ 2 : 0,59 ;
question matérielle : 0,79 ; conflit de préférence confirmé : 0,19), puis un seul
arrondi `Decimal / ROUND_HALF_UP` à 0,01. `unknown` n'impose pas 0,19.
Les anciens codes injectés dans une annotation n'ont aucune autorité numérique.
Les références, preuves, neutralisations, couverture, citations et explications
ne modifient jamais les décisions ou le résultat et ne provoquent aucune relance.
Les exceptions des vérificateurs et convertisseurs restent des observations.

## Raccordements et sauvegarde

- `build_fit_context` sélectionne prompt, template, schéma et politique versionnés.
- `build_fit_user` et `complete_fit` utilisent les mêmes faits et la même source
  immuables ; le budget vérifie le schéma réellement envoyé, sans troncature.
- Le Batch utilise la source `item.analyzer_input` pour la requête, la comparaison
  à l'import et le diagnostic. Le lecteur est sélectionné par schéma enregistré.
- Le cache existant dépend des messages et du schéma : aucun cache parallèle.
- Le recalcul autorisé conserve son plan figé et vérifie la source dans
  `already_compatible`. Il écrit atomiquement les seules données du fit : score,
  raisons, risques, diagnostic et leurs copies publiques dans `raw_response`.
- Une raison positive par axe est retenue si elle existe, avant de compléter
  jusqu'à cinq raisons distinctes. Les qualifications des preuves sont conservées.

Les templates v5 sont conservés dans `templates/job_fit_v5`. Les anciens contextes
rendent leur template figé sans nouveau bloc source ; les lecteurs/calculs
historiques ne remappent pas les anciennes réponses en décisions v6.

## Acceptation de l'extraction : référence fixée de main

Les deux durcissements par rapport à `main` **6a9309d** sont retirés pour
`OfferAnalysis` uniquement : en immédiat, le contenu valide selon Pydantic décide
de l'acceptation, sans nouveau veto `finish_reason/refusal` ; à l'import et dans
le cache Batch, la couche JSON Schema supplémentaire ne rejette plus un champ
omis disposant d'une valeur par défaut Pydantic. Le Batch continue de rejeter
les erreurs de transport, refus et sorties signalées incomplètes comme dans main.
Les autres schémas de candidature gardent leurs contrôles actuels.
Un échec de publication/contexte après lecture d'un cache valide est propagé
sans supprimer ce cache et sans provoquer de nouvelle extraction.

Le contrôle local des `supporting_excerpts` cherche d'abord la citation telle
quelle, puis retire si nécessaire des paires de guillemets extérieurs reconnues
(droits, français ou typographiques), y compris imbriquées. Il conserve le
passage exact de l'annonce, avec ses espaces et ses guillemets éventuels. Les
différences de casse, de ponctuation et les paraphrases restent rejetées ; un
retrait réel produit toujours une observation. Ce contrôle commun couvre
l'immédiat, le Batch, les réponses du cache et les extractions explicitement
demandées lors d'un recalcul. Il ne change ni le prompt ni le schéma ni les clés
du cache et ne restaure pas automatiquement les citations historiques.

## Limites et validation

`tests/test_fit_decisions.py` couvre le calcul, les quatre entrées nécessaires,
les troncatures et doublons, le non-blocage, les sources et versions, le routage
immédiat/Batch/cache, la sauvegarde et les comportements d'extraction de la
référence fixée. Les tests historiques continuent de vérifier leurs anciens
codes/plafonds. Les tests de candidature vérifient leurs contrats existants.

Ces tests utilisent exclusivement des diagnostics simulés. Ils prouvent le
fonctionnement du code et les données envoyées, pas une amélioration mesurée du
jugement LLM. Une extraction historique erronée reste inchangée pour les autres
consommateurs. Les ex æquo et la zone inaccessible 60–74 % subsistent. Les raisons
corrigées pourront influencer les appels futurs qui les utilisent, notamment
`application_draft` et `motivation_letter_repair`, sans régénération automatique.
Aucune migration, ouverture d'application ou nouvelle version ne lance de
recalcul, import ou génération. Les 589 anciennes réponses ne mesurent pas v6.

Validation locale du 8 octobre 2026 : `make check` réussi, **2 713 tests**
(hors marqueurs `integration` et `llm`) et Ruff sans erreur ; `git diff --check`
réussi. Aucun appel fournisseur réel, recalcul historique, import en production,
régénération de candidature réelle ou rebuild n'a été lancé pour cette révision.

---

## Documentation historique — v5 (ne décrit pas le nouveau contrat)


## Fonctionnement

L’extraction de l’offre reste indépendante du candidat. L’évaluation reçoit ensuite
la projection factuelle complète du profil, un résumé d’expérience déclaré dont
la source est `DECLARED_EXPERIENCE`, l’offre structurée et des références compactes.
Les expériences et projets gardent leur ordre, leur contexte, leur statut, leurs
`allowed_claims` et leur `evidence_level`. La sélection éditoriale du CV, ses styles,
la date du jour et les anciennes notes ne sont pas envoyés au scoring.

Le LLM retourne un `JobFitEvaluation` interne : trois niveaux entiers de 0 à 4,
leurs constatations, les problèmes avec leur origine et les neutralisations avec
leur portée. Il ne retourne aucune note finale. Python calcule
`(50 × missions + 30 × essentials + 20 × responsibilities) / 400`, applique
le minimum des plafonds et arrondit avec `Decimal` / `ROUND_HALF_UP`.

Le schéma strict `JobFitEvaluation` guide la **génération**, mais ne conditionne
jamais la lecture des niveaux. `fit_response.read_fit_response` lit séparément les
trois entiers non ambigus de 0 à 4. Des annotations absentes, des types incorrects,
une origine manquante, un libellé inconnu ou des champs supplémentaires ne bloquent
pas un résultat contenant ces trois niveaux. Une note libre `fit_score` est ignorée.

Les contrôles de références, de preuves, de couverture, de neutralisation et de
cohérence sont **définitivement consultatifs**. Le score est calculé avant leur
exécution. Chaque contrôle reçoit une copie des données ; une exception ou un
retour incorrect devient une observation dans `diagnostic_checks.warnings`.
Ces contrôles, y compris ceux ajoutés ultérieurement, ne refusent pas la note, ne
changent pas les niveaux, n'ajoutent aucune pénalité et ne déclenchent pas de nouvel
appel. Les références Skill et bullet peuvent être citées ensemble sans transformer
la Skill en réalisation. Le diagnostic original est conservé, sans réparation
sémantique silencieuse.
`diagnostic_checks.mode` vaut `advisory` et son statut vaut `ok` ou `warnings`.

| Règle | Résultat |
| --- | --- |
| Conflit ferme de préférence | Plafond 0,19 |
| Travail dominant éloigné / missions ≤ 1 | Plafond 0,39 |
| Problème substantiel / travail indéterminable / un axe ≤ 2 | Plafond 0,59 |
| Question centrale importante | Plafond 0,79 |
| Réserve limitée ou clarté du contexte | Aucun plafond automatique |
| Skill déclarée sans projet associé | Source valable, aucun plafond de source |

Le résultat public conserve les trois champs `fit_score`, `match_reasons`, `risks`.
Le diagnostic est stocké séparément dans `job_analyses.fit_diagnostics` : réponse
brute originale (`evaluation` et texte exact `original_response` lorsqu'il est
disponible), niveaux utilisés (`used_levels`), observations, faits
immuables, références, extraction disponible, prompts, schéma, paramètres, politique,
empreintes, calcul et horodatage. Les références historiques se résolvent dans cette
copie des faits, indépendamment des modifications futures du profil.

Les calculs immédiat, Batch et cache passent par les mêmes fonctions. Un lot garde
son contexte dès sa création. Un changement factuel ou de politique bloque sa
publication. Un changement de style ou de sélection du CV ne change pas ce contexte.
L’import Batch inspecte le schéma enregistré à titre consultatif pour les diagnostics
à trois niveaux. Un schéma manquant ou inconnu ne bloque pas trois niveaux
identifiables dans un contexte figé. Si le schéma et les niveaux sont tous deux
inidentifiables, la requête reste en échec explicite. Un ancien résultat compatible
avec son schéma enregistré conserve sa provenance ancienne ; ses niveaux ne sont
jamais inventés.

Une réponse contenant trois niveaux exploitables produit un score, même si son
diagnostic ne respecte pas le schéma de génération. Le prompt précise les types de
preuves par axe, distingue compétences
déclarées et réalisations, et fournit une liste de couverture des exigences. Les
années sont interprétées par le LLM : aucune grammaire Python ne prétend reconnaître
ou rejeter les formulations d’ancienneté.

Une réponse illisible ou sans trois niveaux exploitables produit une erreur explicite,
sans score inventé et sans appel de correction automatique. L'ancien résultat réussi
est conservé en cas d'échec d'un recalcul. Un indicateur fournisseur de refus ou de
sortie incomplète reste une observation si le contenu contient effectivement les
trois niveaux exploitables ; un contenu réellement tronqué qui empêche leur lecture
reste inexploitable. Seul un cache privé des données minimales peut être rafraîchi
de manière ciblée ; les erreurs d'annotations ne justifient jamais son éviction.

Si le JSON est coupé dans une annotation, le lecteur commun peut récupérer son
préfixe structuré. Il conserve les valeurs entièrement lues, les doublons et les
codes métier déjà complets ; il ne ferme pas artificiellement un texte ou un
nombre. Un nombre sans délimiteur à la coupure, un niveau absent ou un doublon
contradictoire ne fournit pas de niveau exploitable. Le texte original exact reste
stocké et la troncature devient une observation, y compris en Batch et depuis le
cache. Une erreur de syntaxe avant la coupure n'est pas réparée.

Les codes métier reconnus sont lus indépendamment de leurs annotations : leur
plafond reste applicable même si l'axe, l'origine ou les références sont incorrects.
Un code inconnu est conservé et signalé, sans plafond inventé. Les vérificateurs ne
peuvent ni ajouter ni supprimer un plafond. Les poids, plafonds, garde-fous et arrondi
restent inchangés ; `evidence_fit_calculation_v2` trace la lecture désormais tolérante
des codes inconnus, et `fit_levels_reader_v1` versionne la lecture des niveaux.

## Diagnostics et recalcul

Depuis la racine du dépôt, avec l’environnement configuré pour la base souhaitée :

```sh
.venv/bin/elan fit-diagnostics --job-id 123
```

Une base ancienne reçoit les deux colonnes nullable au prochain démarrage normal.
Pour les ajouter seules, sans maintenance du workflow ni recalcul :

```sh
.venv/bin/elan fit-schema
```

Une nouvelle installation peut utiliser `elan init-db`. Aucun de ces chemins ne
recalcule les notes existantes.

Préparer une sélection précise, sans appel fournisseur ni écriture des offres :

```sh
.venv/bin/elan rescore --job-id 123 --job-id 456 --dry-run --plan /tmp/elan-fit-plan.json
```

Prévisualiser le périmètre global par défaut :

```sh
.venv/bin/elan rescore --all-active --dry-run --plan /tmp/elan-fit-global.json
```

Ce périmètre comprend les offres actives canoniques avec un fit existant, y compris
les candidatures préparées non envoyées. Il exclut les statuts `sent`, `interview`,
`rejected`, `quality_rejected`, `archived`, les archives horodatées, les candidatures
soumises, les doublons secondaires, les groupes en revue et les traitements réservés.
Les statuts `closed`, `withdrawn`, `removed` sont aussi exclus si une source les
enregistre. Le modèle actuel n’a pas de champ dédié à l’expiration d’une annonce :
un retrait qui n’est pas enregistré ne peut pas être détecté par cette sélection.
`completed_filter_routing.closed_at` décrit la fin du filtrage, pas l’expiration.

Le fichier conserve les IDs exacts, les opérations, les faits, les versions et leurs
empreintes. La sortie indique les offres exclues, déjà compatibles, les évaluations
et les extractions nécessaires. Il contient des données privées et est créé avec
les permissions `0600` ; conserver ce fichier hors Git. Sa création refuse d’écraser
un plan existant.

Les extractions historiques incomplètes ou sans provenance exploitable sont signalées
`needs_extraction`, sans appel supplémentaire. `--include-extractions` permet de les
annoncer explicitement dans un nouveau plan à soumettre à autorisation. Une
représentation historique incompatible reste `unresolved`, même avec cette option.
`--force` inclut explicitement les évaluations déjà compatibles.

**Présenter le plan et obtenir l’autorisation explicite de l’utilisateur avant toute
exécution réelle, même lorsqu’un cache permet de calculer sans coût fournisseur.**
Après cette autorisation uniquement :

```sh
.venv/bin/elan rescore --plan /tmp/elan-fit-global.json --execute-approved
```

L’exécution lit ce fichier, sans refaire une sélection dynamique. Elle ignore et
signale les offres devenues inadmissibles ou modifiées. Elle n’ajoute ni nouvelles
offres ni extractions non annoncées. Chaque publication met à jour atomiquement les
trois champs publics, leurs copies dans `raw_response` et le diagnostic séparé.
Société, localisation, faits historiques de l’offre, statut, dates, score de ranking,
candidatures, documents et choix manuels restent conservés. Un échec conserve l’ancien
fit. Une extraction autorisée apparaît dans les nouveaux faits et leur provenance,
sans réécrire les faits historiques de `raw_response`.

Les plans suivants réutilisent d'abord le snapshot complet de
`fit_diagnostics.offer_facts`, avec son empreinte et sa propre provenance. Les
anciens faits de `raw_response` ne peuvent pas être validés par la provenance
d'une extraction différente. Un repli historique exige une provenance associée à
ces mêmes faits. Un champ optionnel absent dans l'historique n'empêche pas la
réutilisation d'un snapshot récent complet.

En immédiat, chaque offre fige une copie du profil provenant de la source pertinente
au démarrage du traitement. Un Pipeline sans profil explicite consulte le profil
global actualisé ; un profil fourni par l'appelant reste sa propre source. La
requête, le diagnostic et les contrôles d'obsolescence utilisent cette même copie
et vérifient cette même source avant et après l'évaluation. Une modification
factuelle en cours de traitement bloque sa publication ; une modification visuelle
du CV ne crée pas d'obsolescence factuelle.

## Budget et limites

`FIT_MAX_COMPLETION_TOKENS` vaut 12 000 par défaut pour ce diagnostic plus détaillé.
Les autres appels gardent leur budget. La requête complète est vérifiée avant l’appel,
avec son schéma, ses données et une réserve de sortie. Le contrôle utilise une borne
conservatrice en octets UTF-8 et 1 024 tokens de réserve de cadrage, pas un compte
exact de tokenizer. Un dépassement produit une erreur, sans sélection ni troncature
du profil. Un alias de modèle inconnu exige `OPENAI_CONTEXT_WINDOW_TOKENS`.

Les limites connues sont documentées sur les pages officielles
[GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini) et
[GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4).

Mesure historique de la version v4, le 8 octobre 2026, avec le profil synthétique des tests et une petite offre
AI Engineer : système seul **10 748 caractères / 11 220 octets**, données utilisateur
**16 949 octets**, schéma **3 155 octets**, corps de requête Batch assemblé
**33 565 octets**. La borne conservatrice du précontrôle est **34 525 tokens**, plus
**12 000 tokens** réservés pour la sortie. Ces tailles dépendent des données et ne
mesurent pas une amélioration de la qualité du jugement. La version v5 ajoute des
exemples de références et une liste de couverture ; le précontrôle recalcule la
borne de chaque requête avec le prompt réellement utilisé.

Un diagnostic identique produit le même score et les mêmes raisons/risques. Deux
nouveaux jugements LLM peuvent différer. Les poids et plafonds sont une politique
prudente, pas une probabilité de recrutement ni des coefficients statistiquement
validés. Les nouvelles notes n’occupent pas 0,60–0,74 ; sans plafond, les axes 3/4
produisent sept notes hautes et des ex æquo. Les notes historiques restent distinctes.

## Destination des règles

| Règles | Destination et portée |
| --- | --- |
| Activités dominantes, alternatives, exemptions, périmètre avancé d’une Skill, importance centrale/périphérique, étapes accomplies d’un projet | Prompt normatif ; nécessite une interprétation sémantique du LLM |
| Support, niveaux, codes, axes, origine, portée des neutralisations | Schéma de données ; les niveaux doivent être trois entiers de 0 à 4 |
| Références existantes, types de sources, couverture, appui positif pour axes élevés, cohérence niveau/problème | Inspection consultative ; conserve ses observations sans refuser ou modifier la note |
| Ancienneté seule / composante professionnelle d’une exigence mixte | Interprétation dans le prompt, sans expression régulière Python |
| Diplôme/langue obligatoire versus préférence inconnue | Prompt et exemples ; les références de préférence inadmissibles sont signalées dans le diagnostic |
| Poids, plafonds, garde-fous, arrondi | Calcul Python et politique versionnée ; modifier aussi la version du calcul lors d’un changement de règles |
| Contexte figé, diagnostics, schémas historiques, cache ciblé | Application et persistance |
| Périmètre approuvé, extractions annoncées, publication atomique | Plan de recalcul, commande explicite et écriture dédiée |

L’extraction initiale reste une limite : une référence valide à `required_skills[0]`
ne garantit pas la fidélité du champ à l’annonce originale. Ses avertissements sont
conservés. Les tests synthétiques et mocks vérifient ces garanties mécaniques ; ils
ne démontrent pas la justesse sémantique de tous les futurs appels réels.

## Vérification du développement

```sh
make check
```

Les scénarios dédiés sont dans `tests/test_evidence_fit.py`,
`tests/test_fit_nonblocking.py`, `tests/test_fit_recovery.py` et
`tests/test_fit_extraction_snapshots.py` et `tests/test_fit_profile_snapshots.py`, complétés par les tests
du parcours immédiat, du Batch, du cache, de l’admission et du pipeline. Ils utilisent
une base temporaire et des fournisseurs simulés. Aucun appel LLM réel ni recalcul de
la base utilisateur n’est nécessaire à cette vérification. Les tests de non-régression
font traverser des annotations incorrectes aux lecteurs réels, au mode immédiat,
au Batch, au cache et à la sauvegarde. Ils injectent aussi des exceptions et des
mutations dans les futurs contrôles, vérifient l'absence de nouvel appel, la conservation
du barème et la préservation de l'ancien résultat lorsque les niveaux sont absents.

## Cause du problème observé dans v4

Le test réel de 30 offres réparties en cinq tranches a produit 60 réponses complètes
(un appel et une correction pour chaque offre). Toutes étaient compatibles avec le
schéma et contenaient les trois niveaux numériques. Le validateur v4 a pourtant
rejeté les 30 diagnostics finaux : types de preuves/statuts incorrects sur 27 offres,
couverture incomplète sur 21, neutralisations d’ancienneté sur 18, faible niveau sans
problème substantiel conforme sur 15. Plusieurs causes peuvent concerner une offre.

Le refus était global : une observation locale annulait l’ensemble du diagnostic.
Les règles étaient parfois trop strictes, notamment lorsqu’une réalisation
pertinente et une Skill étaient citées ensemble, ou lorsque l’ancienneté comportait
la précision « stage, alternance ou CDI inclus ». Certaines réponses contenaient
aussi des erreurs réelles, telles qu’un diplôme traité comme une préférence. Les
instructions ne donnaient pas assez d’exemples précis de combinaisons autorisées de
références et de statuts. Les tests synthétiques v4 confirmaient les refus attendus,
sans vérifier cette compatibilité sur des réponses réelles.

La version v5 transforme ces contrôles en observations, corrige les accès aux
références inconnues et clarifie le prompt. Cela rétablit la disponibilité des notes ;
cela ne démontre pas, à lui seul, une amélioration du jugement du LLM. Les scores
historiques de la base ne sont pas recalculés par cette modification.

Vérifications de v5 : les 60 réponses v4 sauvegardées produisent localement 60 scores
avec leurs niveaux inchangés, sans nouvel appel. Un contrôle réel v5 sur cinq offres
du même échantillon a ensuite produit cinq scores en cinq appels immédiats, sans
correction ni extraction. Des observations restent présentes sur les cinq diagnostics :
cela vérifie la disponibilité du résultat, pas l’absence d’erreurs sémantiques du LLM.
Une incohérence entre niveau et code de problème est notamment encore possible. Les
plafonds de la consigne restent appliqués aux codes renvoyés, avec leur détail stocké
dans le diagnostic ; les observations consultatives ne constituent pas de plafond.
