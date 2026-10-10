# Installation et démarrage — benchmark Élan 200

## Contenu de l'archive

```text
.agents/skills/skill-evaluation/
├── SKILL.md
└── references/skill-evaluation.md
benchmark-instructions/
├── benchmark-200-instructions.md
├── benchmark-report-design.md
└── run-config-template.md
```

Le dossier `.agents/skills/skill-evaluation/` contient **uniquement la méthode permanente**. Toutes les instructions de campagne, d'arborescence, de suppression/migration, de présentation et le template de configuration sont **hors du skill**, dans `benchmark-instructions/`.

Décompresser l'archive à la **racine du dépôt**, sans écraser des fichiers de skill modifiés localement. Vérifier `git status` avant de commencer.

## Instruction à donner à Codex

```text
Utilise $skill-evaluation.

Lis entièrement :
- benchmark-instructions/benchmark-200-instructions.md
- benchmark-instructions/benchmark-report-design.md
- benchmark-instructions/run-config-template.md

Cette campagne consolide 100 offres historiques et 100 nouvelles offres déjà sélectionnées.
Conserve les sorties originales des runs historiques. Les anciens rapports et comparaisons
seront supprimés seulement après migration/validation des données utiles.

Versionne chaque prompt V1/V2/V3 sans modifier les versions existantes.
Pour chaque run, avant le premier appel API, crée un fichier config.md contenant
la copie intégrale du prompt réellement utilisé et tous les paramètres du modèle.
Enregistre aussi l'enveloppe finale exacte de chaque requête par offre.

Commence uniquement par l'inventaire, le plan de déplacement/réorganisation,
la sécurisation des anciennes sorties et la préparation du référentiel de jugement.
Codex doit être le juge indépendant à partir des sources primaires : n'utilise
ni les sorties des modèles ni l'ancien rapport comme vérité terrain.
N'effectue pas encore d'appels API et ne supprime aucune donnée originale.
Présente les fichiers/déplacements projetés, les contrôles et les limites avant
la phase d'exécution.
```
