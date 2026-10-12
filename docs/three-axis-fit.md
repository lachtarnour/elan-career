> Contrat historique de V4, conservé pour le retour aux trois évaluations.
> Sur cette branche, le contrat actif est décrit dans [Score IA global](evidence-fit.md).

# Évaluation de pertinence `evidence_fit`

L’évaluation produit un indice technique et une description de la fiabilité.
Le LLM juge les capacités ; Python applique la formule aux trois décisions
techniques et signale des avertissements. Le score aide à prioriser les offres,
sans rejet automatique.

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
portée. Une qualification d’accès ou une préférence n’est pas une capacité
technique et ne modifie pas les décisions.
Les transferts sont appréciés selon la capacité commune, la réalisation qui
l’appuie, les différences de contexte et la portée restant non couverte.

```text
score = (35 × missions + 45 × essentials + 20 × responsibilities) / 400
```

La formule applique un seul arrondi final `Decimal / ROUND_HALF_UP` à 0,01,
sans plafond. Ces niveaux ordinaux deviennent un indice de classement : les
écarts numériques ne représentent pas des différences mesurées de capacité,
et l’indice n’est pas une probabilité d’embauche. La moyenne pondérée autorise
la compensation entre axes ; `(4, 0, 4)` donne ainsi `0,55`. Les trois axes restent
visibles avec la synthèse pour conserver cette information.

Une information absente ne prouve ni compétence ni incapacité. L’incertitude est
décrite à son origine et ne devient pas automatiquement une incapacité.
Les années d’expérience demandées seules sont neutres. Une entrée associant une
durée à une capacité neutralise uniquement la durée ; la capacité reste évaluée.
Une alternative satisfaite suffit, une condition s’applique seulement lorsque
sa prémisse est établie, et l’absence d’un élément facultatif ne pénalise pas le
cœur du fit. Aucun bonus de répétition ou de mots-clés n’est prévu.

## Fiabilité descriptive

`reliability` est descriptif : `offer_information`, `candidate_information` et
`traceability` sont des textes, `business_uncertainties` une liste d’éléments à
confirmer. Aucun score de confiance n’est produit. L’absence d’avertissements de
traçabilité ne certifie pas le jugement.

Ce bloc peut être `null`. Une réponse qui ne le contient pas conserve un score
calculable et une information auxiliaire indisponible.

```json
{
  "decision": {
    "missions": "strong_fit",
    "essentials": "strong_fit",
    "responsibilities": "credible_fit"
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

Les contraintes de contexte peuvent être expliquées dans les réserves
descriptives sans modifier les décisions techniques. Ces contrôles
n’interprètent pas la grammaire des exigences pour reprendre le jugement du
modèle.

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
formule, score avant arrondi, score final, fiabilité, avertissements,
faits d’offre et provenance d’extraction. Les copies publiques `fit_score`,
`match_reasons` et `risks` sont publiées avec ce diagnostic. Les explications sont
retenues lorsqu’elles existent, sans inventer les manquantes.

Les empreintes du prompt, du schéma, de la politique, du profil et de la source
identifient la méthode utilisée. Le cache et les checkpoints reposent sur le
contrat effectivement envoyé ; une réponse historique ne devient pas une réponse
du contrat courant par changement de nom. L’immédiat, le Batch, le cache et le
parcours explicite de réévaluation utilisent le même lecteur et le même calcul.

Les évaluations historiques sont conservées avec leur provenance. Le tri explicite
de la colonne IA compare les scores numériques disponibles, dans le sens choisi.
La sélection automatique donne priorité aux évaluations de la politique courante,
puis au score décroissant. Obtenir un indice de la méthode courante nécessite une
nouvelle évaluation LLM explicite ; une extraction réutilisable peut être conservée
si sa source et sa provenance correspondent toujours. Aucun démarrage d’application,
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
