# Parcours d'une offre dans Élan

[Accueil de la documentation](README.md) · [Configuration](setup.md)

Ce guide suit une annonce depuis sa découverte jusqu'au suivi de la candidature. Les opérations ne sont pas toutes automatiques : la recherche et le filtre local préparent les offres ; l'analyse et la génération d'un dossier sont déclenchées pour les offres choisies. Une archive ou un doublon en attente peut interrompre ce parcours.

## Vue d'ensemble

| Étape | Entrée | Résultat principal | Où le voir |
| --- | --- | --- | --- |
| Collecte | Recherche, URL, texte ou formulaire | Annonces brutes et provenance | Recherche |
| Enregistrement | Annonces collectées | Offre normalisée dans SQLite ; copie ou doute de doublon signalé | Doublons, Analyse |
| Filtre local | Offre enregistrée | **Rejeté** ou **À classer** | Analyse, archives |
| Extraction IA | Offre admise à l'analyse | Faits de l'annonce et diagnostic des missions | Analyse |
| Évaluation IA | Faits conservés et profil | Score **IA**, raisons et risques ; état **Analysée** | Offres |
| Dossier | Offre analysée et profil | CV, lettre, état **Prête** | Détail de l'offre, candidatures |
| Suivi | Décision de l'utilisateur | **Envoyée**, **Entretien** ou **Archivée** | Offres, candidatures |

## 1. Rechercher ou ajouter

La recherche de l'application utilise les sources activées : France Travail, LinkedIn via Apify, Welcome to the Jungle et Google Jobs via SerpApi. Les termes peuvent être répartis en requêtes adaptées à chaque source. Le nombre maximal de résultats demandé s'applique à la collecte ; les règles de pertinence interviennent ensuite. Les paramètres de fraîcheur et de localisation sont transmis aux connecteurs qui les prennent en charge. Voir les [guides des sources](sources/README.md).

L'application collecte les sources en parallèle puis enregistre leurs résultats dans SQLite. Une erreur sur une source est signalée sans annuler les résultats des autres. Une annonce peut aussi être fournie manuellement par URL, texte ou champs structurés. L'origine et les données de source restent attachées à l'offre.

**À retenir :** « collectée » ne veut dire ni « pertinente », ni « analysée ». Une recherche peut enregistrer des annonces qui seront ensuite archivées par le filtre.

## 2. Enregistrer et traiter les doublons

Élan nettoie la description, enregistre les champs de l'annonce et recherche les offres déjà connues. Une identité technique ou une preuve stricte de copie peut rattacher la nouvelle annonce à une offre principale. Une ressemblance insuffisante laisse les deux annonces accessibles et demande une décision dans **Doublons**. Les copies confirmées et les offres déjà traitées ne sont pas réanalysées comme de nouvelles offres. Les dossiers existants sont protégés. Voir [les preuves et décisions de doublon](duplicate-review.md).

L'ajout manuel explicite inscrit une dérogation structurée au filtre automatique. Cette dérogation ne découle pas du seul libellé de source `manual` : elle garde la décision de l'utilisateur traçable.

## 3. Appliquer le filtre local

Après l'enregistrement, la recherche lance le filtre déterministe. Il évalue les contraintes connues, dont le rôle, la séniorité, le contrat et la localisation. Une incompatibilité établie archive l'offre avec son motif ; une preuve insuffisante laisse l'offre à classer. La décision est enregistrée dans `job_scores.components.filter_disposition` et ses éléments d'audit.

Avant une analyse, Élan réévalue au besoin les offres dont l'état du filtre est absent ou périmé. Les décisions explicites de conservation et l'historique de candidature restent protégés. La file **Analyse** contient les offres actives admises, sans doublon non résolu dans leur groupe. Le filtre est indépendant du score IA : aucun seuil numérique ne décide ce rejet. Voir [filtrage et états](filter-labels.md).

## 4. Analyser l'offre

L'utilisateur sélectionne les offres dans **Analyse** et lance une **analyse immédiate** ou une **analyse par Batch**. Les deux modes partagent les mêmes contrats métier et les mêmes contrôles avant publication. OpenAI et Anthropic sont configurables ; le mode Batch nécessite un fournisseur compatible. Les analyses terminées entrent dans `job_analyses`. Un Batch conserve son journal pour reprendre les étapes interrompues et éviter une nouvelle soumission lorsque l'état d'un envoi est incertain. Voir [analyse Batch](batch-analysis.md) et [fournisseurs IA](llm-providers.md).

L'analyse a trois appels structurés :

1. **Préfiltre des missions.** Un appel indépendant produit `mission_screen` à partir du texte de l'annonce. Seules les missions **clairement hors périmètre** et **suffisamment décrites** sont archivées. Les erreurs et incertitudes laissent passer l'offre. Voir [préfiltre des missions](extraction-mission-screen.md).
2. **Extraction des faits.** Le modèle extrait missions, exigences, séniorité, langue et autres faits de l'offre, sans verdict de préfiltre ni profil candidat. Les citations conservées sont vérifiées contre le texte.
3. **Évaluation du profil.** Les faits retenus sont comparés au profil candidat. Le LLM donne directement une note globale `job_analyses.fit_score`, affichée comme score **IA**, et sauvegarde raisons, risques et diagnostics. Le score aide à prioriser ; il ne déclenche pas un rejet automatique. Voir [contrat et calcul de pertinence](evidence-fit.md).

Une offre ou un profil modifié pendant l'opération invalide le résultat devenu obsolète avant sa publication. Une extraction validée peut être reprise depuis un checkpoint compatible ; une analyse complète efface ce checkpoint. Les offres clairement écartées au préfiltre restent consultables dans les archives et peuvent être reprises manuellement.

## 5. Préparer le dossier

À partir d'une offre analysée, la génération adapte les éléments du profil pour le CV, rédige la lettre, applique les vérifications et enregistre une candidature. Les documents comprennent un CV **DOCX** éditable, des sources **HTML** et, lorsque le rendu réussit, les **PDF** du CV et de la lettre. Un échec du rendu PDF laisse le DOCX disponible et ajoute un avertissement. Les fichiers et leurs chemins sont enregistrés avec la candidature. Voir [la sélection des projets](project-selection.md) et la [personnalisation des documents](setup.md#cv-template-and-writing-style).

Le dossier généré est **Prêt** pour relecture. Les diagnostics de validation restent visibles ; la disponibilité du dossier ne signifie pas que la candidature a été envoyée. L'envoi sur le site de recrutement reste une action de l'utilisateur.

## 6. Suivre ou archiver

Après soumission, l'utilisateur renseigne **Envoyée**, puis éventuellement **Entretien** ou **Archivée**. Une offre peut également être ajoutée à la **Top sélection** ou archivée sans candidature. Les états affichés privilégient les preuves enregistrées : une analyse, des documents ou un envoi ne redeviennent pas « Nouvelle » à cause d'un ancien statut. **Nouvelle / À classer** se traite dans **Analyse** ; **Offres** présente les offres analysées et le suivi. Voir [filtrage et état des offres](filter-labels.md).

## Repères dans le code et la base

| Responsabilité | Module ou donnée |
| --- | --- |
| Orchestration des étapes | `smartapply/pipeline/pipeline.py` |
| Collecte, normalisation et enregistrement | `smartapply/pipeline/ingest/`, `smartapply/pipeline/ingestor.py` |
| Preuves de copie et revue | `smartapply/dedup/` |
| Filtre avant analyse | `smartapply/pipeline/process/local_filtering.py`, `analysis_admission.py` |
| Extraction, préfiltre des missions, fit | `smartapply/pipeline/process/analysis.py`, `extraction_screening.py`, `smartapply/llm/fit_context.py` |
| Analyse en lot et reprise | `smartapply/pipeline/batch_analysis/`, `smartapply/database/analysis_batches.py` |
| Adaptation et rendu du dossier | `smartapply/pipeline/applier.py`, `application_renderer.py`, `smartapply/cv/` |
| État visible et suivi | `smartapply/jobsearch/offer_state.py`, `status.py`, `workflow.py` |
| Données principales | `jobs`, `job_scores`, `job_analyses`, `applications`, `generated_documents` |

Les étapes de filtre, d'analyse et de dossier ne sont pas déduites d'un score unique : la base garde leurs décisions, leurs preuves et leurs dates séparément.
