# Préfiltre des missions avant extraction

`job_scope_screening` produit uniquement `mission_screen`, avant toute nouvelle
extraction, indépendamment du profil du candidat et d'`evidence_fit`.
Le prompt reprend à l'identique les instructions du préfiltre auparavant incluses
dans le prompt d'extraction.
Ce champ contient uniquement deux valeurs finies :

| Champ | Valeurs |
| --- | --- |
| `mission_relation` | `direct`, `adjacent`, `clearly_outside`, `unknown` |
| `mission_evidence` | `sufficient`, `insufficient`, `contradictory` |

Le périmètre couvre l'IA, le machine learning, la Data Science, l'analyse de
données, l'analyse quantitative, la recherche quantitative, l'Analytics
Engineering, le conseil data/IA et leurs activités proches.
Il correspond aux termes de recherche : AI Engineer, Machine Learning Engineer,
Data Scientist, AI Research Engineer, Applied ML Scientist, NLP Engineer,
Computer Vision Engineer, Speech AI Engineer, Data Analyst, Analytics Engineer
et Technical AI Consultant. Cette liste situe le périmètre ; elle n'impose
aucun intitulé exact et ne modifie pas les recherches ni le profil.
Le modèle classe le sens des activités, leur objet et leurs livrables :
synonymes, formulations équivalentes et missions proches restent acceptés,
dans toute langue ou secteur, même sans les mots « IA », « ML » ou « data ».
Prévoir la demande à partir d'historiques de ventes, analyser les usages d'un
produit ou consolider des informations métier en jeux de données fiables peut relever
du périmètre avec un autre intitulé. La présence d'un mot-clé ne suffit pas
non plus à conserver une offre : ses responsabilités doivent établir ce lien.
Le classement décrit les responsabilités et les livrables réellement confiés :

- `direct` : responsabilité substantielle sur des modèles ou systèmes IA/ML,
  recherche IA, NLP, vision ou parole, traitement et analyse de données,
  analyse et recherche quantitatives
  (modélisation, prévision et expérimentation statistique),
  produits BI/analytiques ou conseil data/IA concret. L'analyse métier et le
  conseil fonctionnel data restent pertinents sans ML ni programmation :
  cadrage de cas d'usage, gouvernance, qualité des données, accompagnement de
  la réalisation ou de l'adoption d'une solution data/IA identifiable.
  Pour les fonctions financières ou économiques, les missions doivent établir
  un travail de recherche ou d'analyse quantitative. Les tâches financières,
  comptables, fiscales ou de suivi budgétaire seules ne suffisent pas.
  Cette distinction porte sur le travail confié, sans exclure un secteur entier.
- `adjacent` : contribution substantielle à ce périmètre : Data Engineering,
  MLOps/LLMOps, exploitation d'une plateforme data/IA, intégration de services
  data/IA dans un logiciel ou pilotage de leur réalisation. Le lien data/IA
  doit être décrit dans les responsabilités. Une composante substantielle
  dans un poste mixte suffit à conserver l'offre.
- `clearly_outside` : les missions décrivent concrètement un autre travail,
  sans composante substantielle dans ce périmètre. La preuve est la nature
  du travail décrit, pas l'absence de mots-clés IA. Un poste logiciel, cloud,
  infrastructure ou cybersécurité générique peut être éloigné si ses missions
  concrètes n'ont pas de responsabilité data/IA substantielle.
- `unknown` : le texte ne permet pas d'établir le travail réellement confié.

Les compétences générales transférables, le titre d'ingénieur, le secteur,
l'innovation, les rapports courants et l'utilisation accessoire d'Excel, d'un
tableau de bord, d'un SIG ou d'un assistant IA ne suffisent pas à établir une
proximité. Analyser des données comme responsabilité réelle reste pertinent,
même avec Excel, sans programmation et dans un domaine industriel ou médical.
Saisir des dossiers, suivre les coûts/délais d'un projet ou consulter des
rapports pour exercer un autre métier ne suffit pas. Construire des outils
BI/data ou réaliser une analyse statistique substantielle reste dans le
périmètre. Les exemples de métiers éloignés ne
constituent jamais une liste d'exclusion fondée sur le titre ou le secteur.

`sufficient` signifie que les activités et leur objet ou leurs livrables sont
assez concrets pour classer les missions ; une annonce courte peut suffire.
`insufficient` concerne les missions vagues ou absentes. `contradictory`
concerne uniquement des descriptions de missions incompatibles empêchant le
classement. Un poste mixte ou une contradiction de salaire, de contrat ou
d'expérience ne suffit pas à rendre les missions contradictoires.

Pour les nouveaux appels, `mission_screen` est obligatoire et ne peut pas être
`null`. Si les missions sont impossibles à classer, le modèle répond
`unknown` avec `insufficient` (ou `contradictory` si l'annonce se contredit).

L'offre est archivée avant extraction uniquement lorsque les missions sont
`clearly_outside` **et** que leur description est `sufficient`. Tout autre
résultat poursuit vers `job_offer_extraction`, puis l'évaluation de pertinence.
Une réponse de préfiltre invalide ou une erreur API laisse passer l'offre.
Le résultat, y compris un échec fail-open, est conservé dans `scope_checkpoints`
pour éviter un nouvel appel lors d'une reprise du même contexte.

Les offres ajoutées ou reprises manuellement et les offres ayant déjà une
analyse ou une candidature restent protégées. La décision et ses deux valeurs
sont enregistrées dans `job_scores.components.extraction_screen`. Une offre
écartée est visible dans les archives et peut être reprise manuellement.

Le préfiltre ne calcule aucun score. Ses valeurs ne sont pas transmises à
`evidence_fit`, à `OfferAnalysis` ou aux documents de candidature. Le mode
immédiat et le Batch utilisent la même décision, y compris lors d'une reprise
depuis le cache ou un checkpoint.

Le nouveau contrat d'extraction est `OfferExtractionOutput`, avec les mêmes
champs factuels et catégories qu'auparavant, sans `mission_screen`. Le verdict
n'est transmis ni au prompt ni au schéma d'extraction.

Les réglages indépendants sont exposés dans la page Configuration et dans
`model_config.json`, la source unique de configuration des appels :

- `job_scope_screening` : Anthropic, `claude-haiku-5-5`, `high` dans la configuration de référence.
- `job_offer_extraction` : OpenAI, `gpt-5.4-mini`, `medium` dans la configuration de référence.

Chaque entrée contient `provider`, `model`, `reasoning` et `max_output_tokens`.
Une configuration absente ou invalide produit une erreur explicite avant l'appel.
Les clés de cache distinctes incluent provider, modèle, effort, prompt complet
et schéma : une nouvelle version des instructions change la clé.
Les nouveaux lots figent un `scope_context` et terminent toutes leurs vagues de
préfiltrage avant de préparer les extractions des seules offres conservées.
L'archivage, les overrides manuels et les protections de candidatures utilisent
la logique existante. Aucune réévaluation automatique de l'historique n'est lancée.
