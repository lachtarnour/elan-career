# Analyse par Batch

[Accueil de la documentation](README.md) · [Parcours d'une offre](parcours-offre.md) · [Fournisseurs IA](llm-providers.md#batch-et-suivi)

Le filtre **Nouvelle** de la page **Offres** regroupe les offres actives à classer. Sélectionnez les offres, puis lancez **Analyse par Batch** (mode économique) ou **Analyse immédiate** pour cette sélection. Les deux modes appliquent le filtre déterministe et les mêmes contrats d'extraction et d'évaluation. Une offre déjà analysée, dotée d'une candidature ou liée à un doublon non résolu n'entre pas dans la file courante. Aucun classement numérique ni ancien « Top 25 » ne limite le nombre d'offres analysables.

L'analyse comprend trois étapes structurées :

1. **Préfiltre des missions**, indépendant du candidat. Seules les missions clairement hors périmètre et suffisamment décrites sont archivées.
2. **Extraction** des faits et exigences des annonces conservées, sans recevoir le verdict du préfiltre.
3. **Évaluation** de ces faits par rapport au profil candidat. Seule une analyse complète est publiée dans `job_analyses`.

Le mode immédiat exécute les trois étapes pour chaque offre et publie chaque analyse terminée. Le mode Batch soumet chaque étape à **OpenAI Batch** ou **Anthropic Message Batches** selon le fournisseur configuré. L'extraction attend la fin du préfiltre ; l'évaluation commence après validation des extractions réussies. Une clé API du fournisseur choisi est nécessaire. Les différences de transport, de récupération d'un envoi ambigu et d'estimation des coûts sont décrites dans [Fournisseurs IA](llm-providers.md#batch-et-suivi).

## Travail dans la page Analyse

La liste affiche notamment l'entreprise, le poste, l'expérience, le lieu, la date de publication et l'état d'analyse. Cliquer une ligne ouvre sa description complète et les raisons de classification ; cocher la ligne l'ajoute à la sélection. La recherche, le tri et la sélection restent en place lors des mises à jour passives. La lecture de l'offre dans **Analyse** ne modifie pas le dossier ou les notes ouverts dans **Offres**.

La zone de suivi montre le parcours sélectionné, son état, le nombre de lots et les offres analysées. **Voir les détails** présente l'avancement par étape, les dernières mises à jour et les échecs individuels. Plusieurs parcours Batch peuvent avancer en parallèle sur des offres distinctes, en même temps qu'une analyse immédiate. Le compteur de requêtes fournisseur peut différer du nombre d'offres : les copies exactes peuvent partager une requête, le cache évite certains appels et les reprises ajoutent des tentatives. Il n'existe pas de position fiable dans la file ni d'heure exacte de fin.

## Suivi, reprise et annulation

**Suivi**, dans la navigation principale, permet de consulter les analyses Batch et les créations après réouverture, indépendamment du filtre des offres. Le journal local est affiché avant la synchronisation avec le fournisseur et reste consultable si cette synchronisation échoue. Élan vérifie les parcours actifs toutes les deux minutes, même lorsque la page n'est pas visible. **Actualiser** déclenche une vérification immédiate. Les lots déjà soumis continuent chez le fournisseur lorsque l'application est fermée ; l'import local et le lancement de l'étape suivante reprennent à la réouverture, sans service supplémentaire installé en arrière-plan. Fermer l'application n'annule pas un Batch ; un envoi initial encore en cours est enregistré avant la fermeture.

La fermeture arrête l'analyse immédiate : aucune nouvelle étape d'analyse ne commence après la demande d'arrêt. Élan attend le retour des appels déjà en cours pour enregistrer leurs résultats valides, puis ferme. Les analyses complètes, le préfiltre et les extractions intermédiaires restent sauvegardés. Les résultats devenus obsolètes restent soumis aux contrôles de source et de profil.

**Reprendre** renvoie uniquement les requêtes en échec. Une extraction valide survit à une interruption pendant l'évaluation. **Arrêter** avant l'envoi ne crée pas de lot ; après l'envoi, Élan demande l'annulation au fournisseur et attend sa confirmation. Les analyses et résultats partiels déjà importés sont conservés. Une annulation empêche la soumission d'une nouvelle étape pour ce parcours et n'affecte pas les autres parcours.

Le journal Batch sauvegarde les requêtes, résultats et états nécessaires à la reprise. L'import répété ne publie pas deux fois un résultat et ne double pas son usage. L'offre, le profil et l'historique du groupe sont revérifiés avant envoi, avant évaluation et avant publication ; un résultat devenu obsolète est écarté et signalé.

Si le fournisseur n'a pas répondu à l'envoi d'un lot, Élan affiche **Envoi à vérifier** et utilise la procédure de récupération propre au fournisseur. Il n'envoie pas automatiquement une seconde fois les requêtes payantes. Voir [le cas d'envoi incertain](llm-providers.md#envoi-dont-la-réponse-est-perdue).

## Checkpoints

Chaque tentative de préfiltre est conservée dans `scope_checkpoints`, avec sa clé, son résultat ou son erreur fail-open. Une reprise identique ne relance pas cette étape.

### Extraction

Une extraction validée en attente d'évaluation reste dans `analysis_checkpoints`. Les deux modes la réutilisent uniquement si le modèle, le fournisseur, les paramètres d'effort, les prompts, le schéma et l'entrée normalisée correspondent. L'expiration du cache de réponse n'efface pas ce checkpoint. Une analyse complète le retire. Un résultat du parcours actuel peut aussi être retrouvé dans son journal Batch par sa clé de requête exacte.

## Repères dans le code

| Module | Responsabilité |
| --- | --- |
| `smartapply/llm/batch.py`, `smartapply/llm/anthropic_batch.py` | Transport, états et lecture des résultats Batch |
| `smartapply/database/analysis_batches.py` | Parcours, éléments et journal d'envoi durables |
| `smartapply/pipeline/process/analysis_admission.py` | Admission des offres et protection de leur historique |
| `smartapply/pipeline/batch_analysis/coordinator.py` | Passage entre les étapes, reprise et annulation |
| `smartapply/pipeline/batch_analysis/results.py` | Validation et import des résultats |
| `smartapply/pipeline/batch_analysis/presentation.py` | Progression et liste des offres admissibles |
| `smartapply/desktop/analysis_services.py`, `analysis_controller.py` | Actions et suivi depuis l'application |
| `smartapply/desktop/qml/pages/AnalysisPage.qml` | Espace Analyse |
