# Historique des versions

## 0.2.0

Cette version étend le parcours local de recherche d'emploi, de l'admission des offres à la préparation des candidatures. Elle regroupe les changements depuis `v0.1.0`.

### Nouveautés

- Analyse des offres en trois étapes configurables : préfiltre des missions, extraction des faits, puis évaluation de l'adéquation avec le profil. Les appels immédiats et Batch conservent leur provenance et peuvent être repris selon leur état.
- Diagnostic d'adéquation appuyé sur les faits de l'offre et le profil figé au moment de l'appel. Les diagnostics incertains restent informatifs et ne bloquent pas automatiquement un score.
- Recherche par familles de métiers IA et data, ajout manuel explicite, gestion des doublons et archivage depuis l'espace Analyse.
- Interface Analyse et Offres réorganisée pour rendre les états, l'historique et les détails plus lisibles tout en préservant la sélection et les notes pendant les actualisations passives.
- Relecture et correction des textes de candidature séparées. Les routes de modèles restent configurables.
- Index public de métadonnées des benchmarks historiques, avec vérificateur de sommes de contrôle. Il ne contient ni annonces, ni profil candidat, ni requêtes ou réponses des fournisseurs.

### Corrections et fiabilité

- Conservation des décisions de classification et de revue des doublons lors des traitements ultérieurs.
- Reprise plus prudente des Batch Anthropic lorsque l'acceptation d'une soumission est incertaine.
- Actualisation forcée des appels d'analyse sans réutilisation des checkpoints ou réponses mis en cache, lorsqu'une réanalyse est demandée.
- Normalisation des faits de contrat Welcome to the Jungle et outils de réparation encadrés.

### Mise à jour depuis 0.1.0

- La configuration des modèles par défaut évolue. Les installations existantes doivent contrôler leurs routes dans la page de configuration avant des appels payants.
- La structure interne des faits d'entreprise utilise `source_company`. Conserver une sauvegarde de la base et du profil avant de remplacer une installation existante ; suivre [le guide de configuration](docs/setup.md).
- Python 3.10+ et macOS 13+ restent les prérequis annoncés. Installer depuis les sources avec `make install-desktop`, puis `make run-desktop`. La construction locale du bundle se fait avec `make build`.

### Vérification de publication

- `make check`
- `python3 benchmarks/elan_200/publication/verify.py`
- Vérification du wheel Python et du bundle macOS avant diffusion d'un binaire.
