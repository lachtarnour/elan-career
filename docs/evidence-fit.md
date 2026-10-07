# Évaluation de pertinence `evidence_fit_v4`

## Fonctionnement

L’extraction de l’offre reste indépendante du candidat. L’évaluation reçoit ensuite
la projection factuelle complète du profil, un résumé d’expérience déclaré dont
la source est `DECLARED_EXPERIENCE`, l’offre structurée et des références compactes.
Les expériences et projets gardent leur ordre, leur contexte, leur statut, leurs
`allowed_claims` et leur `evidence_level`. La sélection éditoriale du CV, ses styles,
la date du jour et les anciennes notes ne sont pas envoyés au scoring.

Le LLM retourne un `JobFitEvaluation` interne : trois niveaux entiers de 0 à 4,
leurs constatations, les problèmes avec leur origine et les neutralisations avec
leur portée. Il ne retourne aucune note finale. Python valide le diagnostic puis
calcule `(50 × missions + 30 × essentials + 20 × responsibilities) / 400`, applique
le minimum des plafonds et arrondit avec `Decimal` / `ROUND_HALF_UP`.

| Règle | Résultat |
| --- | --- |
| Conflit ferme de préférence | Plafond 0,19 |
| Travail dominant éloigné / missions ≤ 1 | Plafond 0,39 |
| Problème substantiel / travail indéterminable / un axe ≤ 2 | Plafond 0,59 |
| Question centrale importante | Plafond 0,79 |
| Réserve limitée ou clarté du contexte | Aucun plafond automatique |
| Skill déclarée sans projet associé | Source valable, aucun plafond de source |

Le résultat public conserve les trois champs `fit_score`, `match_reasons`, `risks`.
Le diagnostic est stocké séparément dans `job_analyses.fit_diagnostics` : faits
immuables, références, extraction disponible, prompts, schéma, paramètres, politique,
empreintes, calcul et horodatage. Les références historiques se résolvent dans cette
copie des faits, indépendamment des modifications futures du profil.

Les calculs immédiat, Batch et cache passent par les mêmes fonctions. Un lot garde
son contexte dès sa création. Un changement factuel ou de politique bloque sa
publication. Un changement de style ou de sélection du CV ne change pas ce contexte.
L’import Batch vérifie le schéma enregistré dans chaque requête. Un ancien résultat
valide conserve sa provenance ancienne ; ses niveaux ne sont jamais inventés. Une
requête sans schéma identifiable reste en échec et nécessite une reprise explicite.

Une réponse invalide n’est pas publiée. Le mode immédiat autorise une seule correction
avec les mêmes faits et les erreurs précises, puis signale l’échec. Le Batch marque
l’élément en échec, sans nouvelle soumission automatique. Les réponses refusées ou
tronquées restent des échecs même si leur JSON semble utilisable. Seule l’entrée de
cache concernée est écartée lorsqu’elle est invalide.

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

Mesure du 8 octobre 2026, avec le profil synthétique des tests et une petite offre
AI Engineer : système seul **10 748 caractères / 11 220 octets**, données utilisateur
**16 949 octets**, schéma **3 155 octets**, corps de requête Batch assemblé
**33 565 octets**. La borne conservatrice du précontrôle est **34 525 tokens**, plus
**12 000 tokens** réservés pour la sortie. Ces tailles dépendent des données et ne
mesurent pas une amélioration de la qualité du jugement.

Un diagnostic identique produit le même score et les mêmes raisons/risques. Deux
nouveaux jugements LLM peuvent différer. Les poids et plafonds sont une politique
prudente, pas une probabilité de recrutement ni des coefficients statistiquement
validés. Les nouvelles notes n’occupent pas 0,60–0,74 ; sans plafond, les axes 3/4
produisent sept notes hautes et des ex æquo. Les notes historiques restent distinctes.

## Destination des règles

| Règles | Destination et portée |
| --- | --- |
| Activités dominantes, alternatives, exemptions, périmètre avancé d’une Skill, importance centrale/périphérique, étapes accomplies d’un projet | Prompt normatif ; nécessite une interprétation sémantique du LLM |
| Support, niveaux, codes, axes, origine, portée des neutralisations | Schéma strict et validateur |
| Références existantes, types de sources, couverture, appui positif pour axes élevés, cohérence niveau/problème | Validateur mécanique ; ne prouve pas que les textes cités impliquent le jugement |
| Ancienneté seule / composante professionnelle d’une exigence mixte | Prompt ; vérification mécanique conservative de formes explicites, couverture du reste obligatoire |
| Diplôme/langue obligatoire versus préférence inconnue | Prompt ; le validateur interdit les neutralisations de préférence sur `required_skills` et les références de préférence inadmissibles |
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

Les scénarios dédiés sont dans `tests/test_evidence_fit.py`, complétés par les tests
du parcours immédiat, du Batch, du cache, de l’admission et du pipeline. Ils utilisent
une base temporaire et des fournisseurs simulés. Aucun appel LLM réel ni recalcul de
la base utilisateur n’est nécessaire à cette vérification.
