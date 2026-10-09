# Configuration des appels IA

Élan lit les routes IA uniquement dans le fichier actif `model_config.json`.
`.env` contient les clés API et les paramètres généraux de l'application.
La page **Réglages → Modèles IA** permet de modifier le JSON sans
reconstruire l'application.

Chaque entrée exige `provider`, `model`, `reasoning` et `max_output_tokens` :

| Appel | Provider initial | Modèle initial | Reasoning | Tokens max. |
| --- | --- | --- | --- | ---: |
| `job_scope_screening` | Anthropic | `claude-haiku-5-5` | `high` | 16000 |
| `job_offer_extraction` | OpenAI | `gpt-5.4-mini` | `medium` | 16000 |
| `job_fit_assessment` | OpenAI | `gpt-5.4-mini` | `medium` | 32000 |
| `cv_adaptation` | OpenAI | `gpt-5.4` | `medium` | 32000 |
| `application_texts` | OpenAI | `gpt-5.4` | `medium` | 32000 |
| `document_review` | OpenAI | `gpt-5.4` | `high` | 32000 |

La génération des documents expose trois configurations : sélection du CV,
résumé du CV et lettre, puis relecture. Les corrections conservent exactement
le fournisseur, le modèle, le raisonnement et la limite de l'appel initial :
`cv_selection_correction` hérite de `cv_adaptation`, et
`motivation_letter_repair` hérite de `application_texts`. Une seconde relecture
réutilise `document_review`. Leurs noms restent distincts dans l'historique des
appels, sans réglage indépendant. Les anciennes entrées JSON des corrections
sont ignorées et retirées lors de la prochaine sauvegarde des modèles.

Dans la page de configuration, choisir `claude-haiku-5-5` pour le Fit propose
`high` et 60 000 tokens. Choisir les combinaisons initiales ci-dessus
propose aussi leur raisonnement et leur limite correspondants. Ces valeurs
restent modifiables avant l'enregistrement.

Le fichier du dépôt [model_config.json](../model_config.json) est la
configuration de référence. Dans le `.app`, les fichiers actifs résident dans
`~/Library/Application Support/Elan/` : `.env` et `model_config.json`. Leurs
chemins exacts apparaissent dans la page Configuration. Un premier lancement
initialise les fichiers manquants sans écraser les fichiers existants. Si une
configuration devient ensuite absente ou invalide, l'application affiche une
erreur et ne la remplace pas automatiquement.

Une route incomplète ou incompatible bloque son appel. Le message nomme l'étape
et le champ JSON à corriger. Les modèles dont le raisonnement est pris en charge
exigent une valeur explicite compatible. Pour un modèle sans niveau de
raisonnement connu, `reasoning: "auto"` signifie explicitement que ce paramètre
n'est pas envoyé ; aucun niveau n'est inventé. `max_output_tokens` doit être un
entier de 256 à 100 000. Les noms de modèle sont des identifiants API ; le
fournisseur confirme leur existence lors de l'appel.

L'analyse immédiate et Batch valide les trois routes du préfiltre, de
l'extraction et de l'évaluation avant de démarrer. Les nouveaux appels lisent le
JSON courant ; une opération déjà démarrée garde son instantané. Les reprises
Batch valident les routes enregistrées dans leur journal avant tout nouvel
envoi. Les requêtes déjà soumises conservent leur configuration exacte.

**Configuration → Application** édite les clés et paramètres généraux dans le
`.env` actif. Les secrets sont masqués dans l'interface et ne sont pas renvoyés
par le service de configuration. Les sauvegardes remplacent les fichiers de
manière atomique. Les changements de base de données et de chemins prennent
effet après redémarrage ; les nouveaux appels IA voient le prochain état du
JSON sans redémarrage.
