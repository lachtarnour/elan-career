# Évaluation de pertinence `evidence_fit`

L’évaluation produit trois sorties indépendantes : un indice technique, une
admissibilité et une description de la fiabilité. Le LLM juge les capacités et
les conditions ; Python applique la formule aux trois décisions techniques et
signale des avertissements. Le score aide à prioriser les offres, sans rejet
automatique.

## Trois décisions techniques

Le minimum calculable est constitué des décisions `missions`, `essentials` et
`responsibilities` dans l’enveloppe `decision`. Une décision doit appartenir à
l’échelle et être non ambiguë. Aucun autre champ ne conditionne le calcul, la
sauvegarde ou la publication du score.

| Décision | Niveau ordinal | Sens |
| --- | ---: | --- |
| `very_low_support` | 0 | Très peu d’appui pertinent |
| `weak_support` | 1 | Appui périphérique, contribution centrale limitée |
| `partial_core_fit` | 2 | Contribution réelle mais partielle au cœur |
| `credible_fit` | 3 | Contribution pertinente, adaptations proportionnées |
| `strong_fit` | 4 | Appui solide au périmètre demandé |

Les missions évaluent les activités dominantes et livrables. Les essentiels
évaluent les capacités techniques nécessaires à la profondeur demandée. Les
responsabilités évaluent autonomie, complexité, décisions, coordination et
portée. Une qualification d’accès ou une préférence reste dans l’admissibilité.
Les transferts sont appréciés selon la capacité commune, la réalisation qui
l’appuie, les différences de contexte et la portée restant non couverte.

```text
score = (50 × missions + 30 × essentials + 20 × responsibilities) / 400
```

La formule applique un seul arrondi final `Decimal / ROUND_HALF_UP` à 0,01,
sans plafond. Ces niveaux ordinaux deviennent un indice de classement : les
écarts numériques ne représentent pas des différences mesurées de capacité,
et l’indice n’est pas une probabilité d’embauche. La moyenne pondérée autorise
la compensation entre axes ; `(4, 0, 4)` donne ainsi `0,70`. Les trois axes restent
visibles avec la synthèse pour conserver cette information.

Une information absente ne prouve ni compétence ni incapacité. L’incertitude est
décrite à son origine et ne devient pas automatiquement une incompatibilité.
Les années d’expérience demandées seules sont neutres. Une entrée associant une
durée à une capacité neutralise uniquement la durée ; la capacité reste évaluée.
Une alternative satisfaite suffit, une condition s’applique seulement lorsque
sa prémisse est établie, et l’absence d’un élément facultatif ne pénalise pas le
cœur du fit. Aucun bonus de répétition ou de mots-clés n’est prévu.

## Admissibilité et fiabilité

`admissibility` contient deux sous-dimensions, `mandatory_conditions` et
`candidate_preferences`, chacune avec un `status` et des `findings` référencés :

| Statut | Sens |
| --- | --- |
| `compatible` | Conditions pertinentes établies, ou aucune condition pertinente identifiée |
| `incompatible` | Au moins une incompatibilité explicite établie |
| `unknown` | Une condition pertinente reste à confirmer, sans incompatibilité établie |

Un diplôme, un statut étudiant, une habilitation ou une qualification linguistique
explicitement obligatoires appartiennent à `mandatory_conditions`. Les préférences
contraignantes déclarées appartiennent à `candidate_preferences`. Une qualification
non documentée reste `unknown` ; ni qualification ni préférence ne sont déduites
d’un titre ou d’un autre indice indirect. Les connaissances nécessaires au travail
restent évaluées dans les essentiels techniques à leur portée documentée.

`reliability` est descriptif : `offer_information`, `candidate_information` et
`traceability` sont des textes, `business_uncertainties` une liste d’éléments à
confirmer. Aucun score de confiance n’est produit. L’absence d’avertissements de
traçabilité ne certifie pas le jugement.

Ces blocs peuvent être `null`. Une réponse qui ne les contient pas conserve un
score calculable et une information auxiliaire indisponible ; le lecteur
n’invente pas de statut d’admissibilité.

```json
{
  "decision": {
    "missions": "strong_fit",
    "essentials": "strong_fit",
    "responsibilities": "credible_fit"
  },
  "admissibility": {
    "mandatory_conditions": {"status": "compatible", "findings": []},
    "candidate_preferences": {"status": "unknown", "findings": []}
  },
  "reliability": {
    "offer_information": "Les activités principales sont décrites.",
    "candidate_information": "Les réalisations pertinentes sont documentées.",
    "traceability": "Les appuis sont référencés dans les annotations.",
    "business_uncertainties": ["La nature du contrat reste à confirmer."]
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

Cet exemple synthétique donne `0,95`. Les annotations vides donnent lieu à des
avertissements, sans changer les décisions ou le score. Une réponse contenant
seulement son enveloppe `decision` produit la même note.

## Lecture et contrôles informatifs

`read_fit_response` lit les trois décisions indépendamment des autres blocs.
L’enveloppe est placée en premier pour récupérer des décisions entièrement lues
en cas de troncature. Une décision absente, hors échelle ou ambiguë ne permet
pas de calculer une nouvelle note ; aucune valeur n’est inventée. Les doublons
contradictoires et les préfixes de clés échappées sont traités sans masquer une
ambiguïté réelle.

Dès que les trois décisions sont valides, le calcul est effectué. La conformité
auxiliaire, les références, citations, couverture, portée des preuves et
justifications produisent exclusivement des `warnings`. Leur absence, leur
invalidité ou une exception du vérificateur ne peut ni bloquer la publication,
ni modifier, plafonner ou remplacer une décision, ni déclencher une relance.
Les vérificateurs reçoivent des copies ; la réponse et les décisions originales
restent conservées. Une annotation incohérente est signalée sans réparation.

La couverture reconnaît aussi les conditions obligatoires traitées dans
l’admissibilité : une qualification d’accès n’exige pas une pénalité technique
pour être considérée comme examinée. Ces contrôles n’interprètent pas la grammaire
des exigences pour reprendre le jugement du modèle.

## Données, cache et sauvegarde

Le fit utilise `JobFitDecisionEvaluation`, le prompt principal
`smartapply/llm/prompts/templates/job_fit/system.j2` et son template `user.j2`.
Le profil complet et les références de preuves sont transmis dans leur ordre,
sans sélection de CV. Les `allowed_claims` et le niveau de preuve limitent la
portée des réalisations : un projet en cours ne démontre pas ses résultats
futurs. Une compétence déclarée suffit pour une connaissance ou un usage
ordinaire, sans prouver une portée avancée ou opérationnelle.

Le corps normalisé `offer_body` associé à l’extraction est transmis une fois.
`fit_source` conserve sa disponibilité explicite, `offer_source:body`, son texte
et son empreinte. En l’absence de source, les faits et extraits restent disponibles ;
aucune réextraction implicite n’est demandée. Une interprétation source documente
une alternative ou une condition sans réécrire les faits partagés.

`fit_diagnostics` conserve la réponse brute, les décisions utilisées, niveaux,
formule, score avant arrondi, score final, admissibilité, fiabilité, avertissements,
faits d’offre et provenance d’extraction. Les copies publiques `fit_score`,
`match_reasons` et `risks` sont publiées avec ce diagnostic. Les explications sont
retenues lorsqu’elles existent, sans inventer les manquantes.

Les empreintes du prompt, du schéma, de la politique, du profil et de la source
identifient la méthode utilisée. Le cache et les checkpoints reposent sur le
contrat effectivement envoyé ; une réponse historique ne devient pas une réponse
du contrat courant par changement de nom. L’immédiat, le Batch, le cache et le
parcours explicite de réévaluation utilisent le même lecteur et le même calcul.

Les évaluations historiques sont conservées avec leur provenance. Elles ne sont
pas numériquement converties et ne sont pas mélangées aux indices de la méthode
courante dans un classement. Obtenir un indice courant nécessite une nouvelle
évaluation LLM explicite ; une extraction réutilisable peut être conservée si sa
source et sa provenance correspondent toujours. Aucun démarrage d’application,
affichage ou avertissement ne déclenche de recalcul automatique.

Le cache LLM conserve sa purge à 15 jours. La preuve des extractions terminées
reste dans les diagnostics, avec `analyzer_input` et son empreinte ; la purge ne
rend donc pas obligatoire une réextraction lorsque cette provenance est encore
exploitable.

## Vérification

Les tests déterministes vérifient les 125 combinaisons techniques, la récupération
des trois décisions, la publication malgré des annotations invalides, la
préservation des réponses, l’absence de relance, ainsi que les raccordements
immédiat, Batch et cache. Ils vérifient le contrat logiciel ; la qualité du
jugement et le choix des poids nécessitent des évaluations indépendantes.
