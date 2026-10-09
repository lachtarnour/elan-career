# Élan

**Espace de recherche d'emploi macOS pour les profils AI Engineer.** Élan réunit la recherche d'offres, leur analyse, la préparation des candidatures et leur suivi dans une application locale construite avec Python, Qt Quick et SQLite.

Le profil et les règles fournis ciblent les métiers IA et data, principalement de niveau junior à intermédiaire. Pour d'autres métiers, il faut adapter le profil, les termes de recherche et les règles locales ; voir [Personnaliser Élan](#personnaliser-élan).

[![macOS 13+](https://img.shields.io/badge/macOS-13%2B-111017?logo=apple&logoColor=white)](#installation)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Licence MIT](https://img.shields.io/badge/Licence-MIT-55bd92)](LICENSE)
[![CI](https://github.com/lachtarnour/elan-career/actions/workflows/ci.yml/badge.svg)](https://github.com/lachtarnour/elan-career/actions/workflows/ci.yml)

## Démonstration

45 secondes, sans son, avec des annotations en français. Les données sont fictives ; la collecte et les réponses IA sont simulées.

https://github.com/user-attachments/assets/b357b04e-eba4-49aa-a6ed-851d0a94e167

## Parcours d'une offre

```text
Recherche ou ajout manuel
  → Enregistrement et contrôle des doublons
  → Filtre local déterministe
  → Analyse : extraction des faits et préfiltre des missions
  → Analyse : évaluation de l'adéquation avec le profil
  → Préparation du CV et de la lettre, à la demande
  → Relecture, envoi par l'utilisateur et suivi
```

1. **Trouver l'offre.** La recherche interroge les connecteurs configurés : France Travail, LinkedIn via Apify, Welcome to the Jungle et Google Jobs via SerpApi. Une offre peut aussi être ajoutée par URL, texte ou formulaire. La collecte précède le filtrage local : une offre trouvée n'est pas encore une offre retenue.
2. **Enregistrer et rapprocher.** Élan normalise la description, conserve la source et compare l'annonce aux offres déjà enregistrées. Une copie certaine peut être reliée à son offre principale ; un rapprochement ambigu passe dans **Doublons** pour décision. Les offres et l'historique de candidature ne sont pas effacés par ce rapprochement.
3. **Appliquer les règles locales.** Le filtre déterministe écarte les incompatibilités établies, par exemple un contrat ou un niveau d'expérience non accepté. Les cas incertains restent **À classer** dans **Analyse**. Un ajout manuel explicite conserve une trace de dérogation au filtre.
4. **Analyser les offres sélectionnées.** En mode immédiat ou Batch, un premier appel IA préfiltre les missions et archive seulement les offres clairement hors périmètre avec des missions suffisamment décrites. Un deuxième appel extrait les faits des annonces conservées ; un troisième les compare au profil ; le calcul de pertinence produit le score **IA** et des explications. Le Batch peut être repris après interruption. Le score ne rejette pas automatiquement l'offre.
5. **Préparer et suivre la candidature.** Depuis une offre analysée, Élan adapte le CV, rédige une lettre, vérifie le dossier et produit des fichiers éditables et consultables. L'utilisateur relit les documents, soumet lui-même la candidature et met à jour son état (**Prête**, **Envoyée**, **Entretien**, **Archivée**).

Les étapes, leurs exceptions, les données enregistrées et les modules responsables sont détaillés dans le [guide du parcours d'une offre](docs/parcours-offre.md).

Élan comprend aussi un espace **Entreprises** pour suivre les sociétés ciblées et [synchroniser une liste CSV privée](docs/company-targeting.md).

## Installation

Prérequis : **macOS 13+** et **Python 3.10+**.

```bash
git clone https://github.com/lachtarnour/elan-career.git
cd elan-career
make install-desktop
```

Suivez le [guide de configuration](docs/setup.md) pour créer votre profil et configurer les fournisseurs, puis lancez :

```bash
make run-desktop
```

Le guide explique aussi le mode IA simulée, l'utilisation depuis les sources et la construction de l'application macOS. Chaque connecteur de recherche a sa [configuration propre](docs/sources/README.md). Les données du profil et l'historique des candidatures sont stockés localement ; les fournisseurs configurés reçoivent les données nécessaires aux recherches et aux fonctions IA.

## Personnaliser Élan

1. **Profil** — Copiez le [profil d'exemple](smartapply/profile/mock_profile/) dans votre répertoire privé selon le [guide de configuration](docs/setup.md#configure-a-profile). Renseignez votre identité, vos expériences, compétences, projets, formation et préférences.
2. **Recherche et filtres** — Adaptez les [familles de rôles](smartapply/pipeline/ingest/role_families.py), les [règles locales](smartapply/filtering/rules.py), ainsi que les contraintes de contrat et de localisation décrites dans le [guide de configuration](docs/setup.md#matching-rules). Le seul changement de profil ne modifie pas ces règles codées.
3. **Documents** — Adaptez le [modèle HTML/CSS du CV](smartapply/cv/templates/cv.html.j2) ou `template_style.json` dans votre profil privé pour le DOCX. Voir la [personnalisation du CV](docs/setup.md#cv-template-and-writing-style).

## Développement et documentation

```bash
make check
```

Les tests utilisent des données fictives et des fournisseurs IA simulés. Voir [Contribuer](CONTRIBUTING.md) pour l'organisation du code et les vérifications, et [l'index des guides](docs/README.md) pour retrouver la documentation par sujet.
