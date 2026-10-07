# Suppression définitive d'une offre prête

Dans **Offres → Prête**, **Supprimer** est placé à côté d'**Archiver**.
La confirmation affiche le titre et l'entreprise. Elle porte sur l'offre,
ses copies **confirmées**, et leurs dossiers. Les candidats doublons encore
en attente restent indépendants et perdent le lien vers l'offre supprimée.

## Frontière de suppression

- `desktop/offer_deletion.py` réserve le groupe, vérifie le statut et orchestre
  les documents et la transaction. Un dossier envoyé, archivé ou utilisé par
  un traitement actif bloque l'opération.
- `database/repository/offer_deletion.py` retire les résultats, scores,
  candidatures, documents, embeddings, consommations LLM attribuées et
  éléments des journaux Batch. Un Batch partagé conserve les autres offres.
- `pipeline/output_deletion.py` vérifie la propriété du répertoire, déplace
  les fichiers avant la transaction et restaure le dossier si elle échoue.
  Les anciens répertoires de rendu temporaires appartenant au même dossier
  sont aussi effacés. Les liens symboliques ne sont jamais suivis.
  Un plan durable permet la reprise après interruption. Le marqueur de
  validation est écrit dans la transaction de suppression, indépendamment
  d'une éventuelle réutilisation des identifiants SQLite.

Une erreur finale d'effacement des fichiers affiche **Nettoyage à terminer**.
Le dossier est retiré des chemins actifs et le prochain démarrage réessaie
l'effacement. Cette erreur n'est jamais présentée comme une suppression
entièrement réussie.

## Caches et limites

Les nouveaux appels mémorisent leur clé de cache dans la consommation LLM,
ce qui permet un effacement ciblé. Les journaux Batch et checkpoints fournissent
aussi leurs clés. Pour les anciens caches sans attribution, la suppression
recherche les résultats enregistrés dans l'analyse et les documents. Des
réponses anciennes remplacées lors d'une régénération peuvent ne plus être
attribuables à une offre ; leur effacement ciblé ne peut pas être garanti.

Il s'agit d'une suppression des données de travail locales, pas d'un effacement
forensique des pages SQLite ou du disque. Les sauvegardes existantes, fichiers
exportés ailleurs et données conservées par les fournisseurs externes ne sont
pas modifiés. Une offre supprimée peut être réimportée par une collecte future.
