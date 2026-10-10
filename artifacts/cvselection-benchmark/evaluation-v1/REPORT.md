# Benchmark CVSelection — évaluation métier indépendante des 120 sorties

## Synthèse

Les trois Batchs ont chacun produit 40 réponses structurées sur les mêmes 40 annonces. Cette réussite technique ne suffit pas à valider la sélection de CV : les écarts principaux portent sur l'activation de projets spécialisés, la fidélité des justifications et le tri des compétences. Après examen à l'aveugle des annonces et du profil, **Claude Haiku 5.5** est le meilleur choix pour ce corpus : 22 premières places seul, 7 ex æquo, aucun défaut critique relevé et 3 défauts majeurs. GPT-5.4 mini gagne seul 6 offres, GPT-5.4 en gagne 5. Cette recommandation vaut pour ces 40 offres et cette appréciation humaine unique ; elle n'établit pas une supériorité universelle.

| Modèle et réglage | Réponses structurées | Score moyen /4* | Offres avec défaut critique | Offres avec défaut majeur | Premières places seul / ex æquo | Coût Batch estimé USD |
|---|---:|---:|---:|---:|---:|---:|
| GPT-5.4, reasoning none | 40/40 | 2,200 | 4 | 26 | 5 / 2 | 0,36937125 |
| Claude Haiku 5.5, reasoning high | 40/40 | 3,575 | 0 | 3 | 22 / 7 | 0,09211650 |
| GPT-5.4 mini, reasoning high | 40/40 | 2,750 | 2 | 11 | 6 / 5 | 1,45981147 |

\* Le score ordinal sert à ordonner les sorties par offre : 4 très bon, 3 bon avec réserves, 2 défaut métier majeur ou couverture faible, 1 plusieurs défauts majeurs, 0 défaut critique. Ce n'est pas un score de recrutement ni une mesure calibrée. Un défaut mineur peut coexister avec 4/4. Une offre peut avoir plusieurs défauts.

En comparaison directe par offre, Haiku devance GPT-5.4 **32 à 5** (3 égalités), et GPT-5.4 mini **27 à 6** (7 égalités). Mini devance GPT-5.4 **23 à 12** (5 égalités). Les différences observées sont importantes sur ce corpus, mais les 40 sorties par modèle viennent d'un seul Batch et le classement métier repose sur un seul évaluateur.

## Méthode et périmètre

- Les attentes métier des 40 offres ont été écrites dans [`reference.md`](reference.md) à partir des annonces originales et du profil courant **avant** la lecture des sorties. Elles décrivent des choix préférés, pas un corrigé unique : plusieurs troisièmes projets et plusieurs présentations de compétences restent acceptables.
- Les 120 sorties ont été mélangées indépendamment par offre sous les étiquettes A/B/C. Les notes et défauts ont été enregistrés dans [`ratings.jsonl`](ratings.jsonl) avant consultation de [`blind_map.json`](blind_map.json). Le style des sorties pouvait malgré tout suggérer un modèle : l'aveugle ne supprime pas complètement ce biais.
- Le jugement porte d'abord sur les trois projets (adéquation et complémentarité), puis sur les compétences exigées, les omissions et le bruit, enfin sur le titre, les certifications et la fidélité des raisons au profil et à l'offre. Les trois projets doivent être distincts. Un projet `disabled` ne peut pas être sélectionné. Un projet `specialized` exige une condition d'activation explicite et une contribution distincte.
- **Mineur** : détail périphérique, justification un peu faible ou catégorie de compétence incorrecte sans modifier le cœur du CV. **Majeur** : mauvais arbitrage central, forte dilution des compétences ou présentation qui peut orienter le CV vers un mauvais métier. **Critique** : violation de la politique de projet ou attribution factuelle précise absente du projet source. Les défauts ont une preuve concrète dans les jugements et les sorties figées.
- Les modèles n'ont jamais servi de référence mutuelle. Le dataset et les sorties brutes n'ont pas été modifiés. [`stats.py`](stats.py) vérifie les SHA-256 des trois sources, l'unicité des 40 offres et des 120 sorties, l'accord entre notes et gagnants, puis calcule [`statistics.json`](statistics.json) et [`comparison-40.md`](comparison-40.md). Aucun appel LLM supplémentaire n'a été effectué.

## Fiabilité et coûts observés

| Modèle | Défauts mineurs / majeurs / critiques | Tokens entrée fournisseur | Cache lu | Tokens sortie, dont raisonnement | Sortie hors raisonnement | Latence par offre |
|---|---:|---:|---:|---:|---:|---|
| GPT-5.4 | 15 / 26 / 4 | 474 645 | 346 880 | 22 174, dont 0 | 22 174 | indisponible |
| Haiku 5.5 | 25 / 3 / 0 | 666 582 | 178 893 | 230 425, dont 192 235 | 38 190 | indisponible |
| GPT-5.4 mini | 28 / 12 / 2 | 474 645 | 377 856 | 626 376, dont 602 266 | 24 110 | indisponible |

Haiku a aussi 4 587 tokens d'écriture en cache. Les compteurs de tokens des fournisseurs ne sont pas directement comparables à cause de la tokenisation et de la comptabilité du cache. Les montants sont la **somme des estimations enregistrées par requête**, non des débits facturés. Aucun temps par offre n'est fourni ; les temps globaux des Batchs sont respectivement 171 s, 177,56 s et 441 s (ordres de grandeur de traitement Batch, non temps de génération par offre). Les 120 réponses ont `error=null` et un objet `structured_response` ; cette fiabilité de transport ne détecte pas les erreurs métier.

## Défauts significatifs avec preuves

1. **Projet désactivé — critique, GPT-5.4, offre 10773 (RL robotique).** La sortie sélectionne `proj_rl_gym` parmi ses trois projets, alors que le profil le marque `disabled`. Le fait que l'annonce demande du RL ne l'autorise pas. Les projets primaires OrthoTwin3D, Math-Consistent Qwen ou SVC permettent des transferts sans franchir cette limite.
2. **Activation spécialisée sans besoin explicite — critique, GPT-5.4.** Offre 11846 (segmentation clients Würth) : `proj_bot_traffic_anomaly` est choisi à partir du contrôle qualité des données clients, sans demande de détection de bots, fraude, abus ou anomalies. Offre 10612 (tracking 3D Harmattan) : `proj_pix2pix_maps` est choisi pour la vision 3D/fusion de capteurs, sans traduction image-à-image ni GAN. Offre 8380 (pricing) : le « performance drift check » des métriques de prévision est assimilé à une mission de détection d'anomalies pour activer le projet bot. Ces rapprochements ne satisfont pas les conditions spécialisées du profil.
3. **Activation spécialisée et valeur complémentaire manquante — critique/majeur, GPT-5.4 mini, offre 7578 (GNC).** Le projet bot est choisi pour « Analyser des logs de vol et participer au diagnostic », qui ne demande pas explicitement de détection d'anomalies. Son champ `complementary_value` reste vide alors qu'il est obligatoire pour un projet spécialisé.
4. **Attribution factuelle inventée — critique, GPT-5.4 mini, offre 11505.** La raison du projet Singing Voice Conversion affirme « a deployed AI pipeline spanning AWS, Flask/FastAPI and model quantization ». Le profil de ce projet décrit une chaîne audio PyTorch en cours, sans ce déploiement ni ces composants attribués à SVC. Ces outils peuvent exister comme compétences générales ailleurs dans le profil, mais ne prouvent pas cette réalisation du projet.
5. **Compétences inutiles — majeur, GPT-5.4, offre 9567 (HPC/multi-GPU).** La sortie ajoute speech-to-text, diarization, Pyannote, computer vision, facial affect estimation, Spark, W&B, etc. Le besoin porte sur calcul, infrastructure et entraînement distribué. La grande liste masque les lacunes HPC réelles. Même phénomène pour GPT-5.4 sur l'offre 9095 (défense, assistance au test), avec audio/vision/forecasting sans mission correspondante. Mini présente aussi du PyTorch/TensorFlow/NLP/DDP pour l'offre Snowflake/ELT 4309.
6. **Titre et certifications.** Sur 11825 (Data Analyst + agents), GPT-5.4 mini propose « Data Analyst & Applied AI Specialist » et une liste large d'ARIMA, séries temporelles et PyTorch ; le titre d'analyste confirmé n'est pas établi par l'identité du candidat. Sur 4309 (Snowflake/ELT), GPT-5.4 sélectionne deux certificats RAG/LangChain alors que la mission est d'abord data engineering. Ces points comptent moins qu'un projet interdit, mais affaiblissent l'adaptation.
7. **Catégorie de compétence incorrecte — mineur, GPT-5.4, offres 8380 et 9095.** `Forecasting` est placé dans `stats_signal`, tandis que le catalogue le range dans `data_analysis`. Les réponses sont structurées mais ne respectent pas exactement le groupement autorisé.

Les cas difficiles ne sont pas pénalisés pour la seule absence d'une compétence réellement absente du profil. Exemples : GCP/BigQuery, Java/Spring, Snowflake, PostGIS ou Kalman ne peuvent pas être inventés. Les projets spécialisés sont acceptés quand l'annonce les active : Pix2Pix pour 9017 et 9561 (génération d'images/GAN), Bot Traffic Detection pour 6760 (anomalies financières), 8292 (fraude), 9509 (détection explicite d'anomalies) et 11200 (anomaly detection secondaire). Le désaccord raisonnable entre deux troisièmes projets primaires conduit à une égalité, comme 7523, 11200, 6658 et 8309.

## Comparaison des 40 offres

Les cellules donnent le score ordinal et le nombre de défauts : C critique, M majeur, m mineur. « Meilleur(s) » conserve les égalités. Les 120 appréciations et leur formulation exacte restent dans `ratings.jsonl`.

| # | Offre | GPT-5.4 | Haiku 5.5 | GPT-5.4 mini | Meilleur(s) | Observation |
|---:|---|---:|---:|---:|---|---|
| 1 | 11845 — Geospatial Data Scientist (UNICC Contract) | 2 (M1) | 4 (m1) | 4 (m1) | Haiku 5.5, GPT-5.4 mini | Même trio primaire raisonnable chez les trois; aucun projet ne prouve GeoPandas/PostGIS. |
| 2 | 9251 — Développeur IA | 3 (m1) | 4 (m1) | 3 (m1) | Haiku 5.5 | Trois projets directs chez tous; Haiku 5.5 distingue correctement métrique de retrieval et qualité des réponses. |
| 3 | 8110 — AI Research Engineer (Kernel & Inference Optimization) | 2 (M1) | 2 (M1) | 3 (m1) | GPT-5.4 mini | Aucun projet ne démontre MSL ni kernels GPU; les transferts multimodaux de GPT-5.4 mini sont les plus cohérents sans prétendre à cette expertise. |
| 4 | 5493 — Consultant Sécurité IA Générative & Cybersécurité IDF H/F | 3 (m1) | 4 (m1) | 2 (M1) | Haiku 5.5 | Haiku 5.5 relie les bons projets tout en séparant retrieval, pentest expérimental et gouvernance sécurité non démontrée. |
| 5 | 10803 — Associate, AI & Robotics | 1 (M1) | 2 (M1) | 3 (m1) | GPT-5.4 mini | OrthoTwin3D et Élan sont utiles; troisième projet ouvert, mais GPT-5.4 mini garde la section compétences concentrée. |
| 6 | 10592 — Founding Analytics Engineer | 3 (m1) | 4 (m1) | 2 (M1) | Haiku 5.5 | Même trio de projets transférables; Haiku 5.5 est le plus honnête sur l'absence de warehouse et le niveau Staff. |
| 7 | 6760 — KERING Finance Data Analyst | 2 (M1) | 4 (m1) | 3 (m1) | Haiku 5.5 | Activation de Bot Traffic Detection acceptable chez les trois; Haiku 5.5 signale la nature synthétique des données et les lacunes Power BI/finance. |
| 8 | 10088 — AI & Service Architect | 2 (M1) | 4 (m1) | 3 (m1) | Haiku 5.5 | Même trio pertinent; Haiku 5.5 est le plus précis sur les limites de l'architecture et du budget. |
| 9 | 11505 — Graduate data scientist | 3 (m1) | 4 (m1) | 1 (C1, M1) | Haiku 5.5 | L'offre vise d'abord la fiabilité des pipelines et des indicateurs; Haiku 5.5 choisit le meilleur troisième projet analytique. |
| 10 | 11878 — Ingénieur IA (F/H) | 4 (m1) | 3 (m1) | 3 (m1) | GPT-5.4 | Les trois choisissent les bons projets; GPT-5.4 a le titre le plus naturel pour la mission logicielle et une justification complémentaire claire. |
| 11 | 11905 — Agentic AI Architect/Lead | 2 (M1) | 4 (m1) | 3 (m1) | Haiku 5.5 | Même trio direct; Haiku 5.5 garde le titre non-Lead et borne explicitement SAP/A2A/MCP. |
| 12 | 3940 — Développeur Fullstack Java/Angular - IA Générative H/F | 3 (m1) | 4 (m1) | 2 (M1) | Haiku 5.5 | Math Qwen et pentest sont deux troisièmes choix raisonnables; Haiku 5.5 privilégie la sobriété et la mise en garde Java/Angular. |
| 13 | 10773 — AI Research Engineer - Robotics | 0 (C1, M1) | 4 (m1) | 3 (m1) | Haiku 5.5 | RL tabulaire reste interdit même lorsque l'offre cherche explicitement le RL; Haiku 5.5 garde la limite robotique explicite. |
| 14 | 6290 — Data Engineer - QuantumBlack, AI by McKinsey | 2 (M1) | 4 (—) | 3 (m1) | Haiku 5.5 | Même trio pertinent; Haiku 5.5 retient les compétences Python/SQL/Spark et qualifie les projets comme transferts plutôt que plateforme data éprouvée. |
| 15 | 11846 — Data Analyst | 0 (C1, M1) | 3 (m1) | 2 (m1) | Haiku 5.5 | Les trois projets de Haiku 5.5/GPT-5.4 mini sont des transferts faibles mais autorisés; GPT-5.4 enfreint la règle spécialisée sur une offre sans anomalie explicite. |
| 16 | 8292 — Security Engineer | 2 (M1) | 4 (—) | 2 (M1) | Haiku 5.5 | Bot Traffic Detection est activé légitimement par la fraude explicitement citée; Haiku 5.5 en borne les données synthétiques. |
| 17 | 9509 — Data Scientist F/M | 2 (M1) | 3 (m1) | 4 (m1) | GPT-5.4 mini | Activation de Bot Traffic Detection possible parce que l'annonce cite anomaly/deformation detection; il ne prouve pas les déformations SAR. |
| 18 | 9017 — Machine Learning Engineer – Video Diffusion Models | 2 (M1) | 4 (m1) | 2 (M1) | Haiku 5.5 | Pix2Pix est activé par image-to-image et GAN explicitement demandés; Haiku 5.5 respecte le fossé vidéo/diffusion. |
| 19 | 8780 — AI Engineer | 4 (m1) | 3 (m1) | 2 (m1) | GPT-5.4 | Math-Consistent Qwen fournit un signal AI Training sans inventer le métier précis; l'incertitude est importante. |
| 20 | 9561 — FR - Generative AI Creative | 2 (M1) | 4 (m1) | 2 (m1) | Haiku 5.5 | Pix2Pix est spécialisé légitime, aucune sortie ne peut montrer le portfolio commercial ou ComfyUI demandé; Haiku 5.5 est la plus honnête. |
| 21 | 10612 — Computer Vision Engineer - Tracker | 0 (C1, M1) | 4 (—) | 3 (m1) | Haiku 5.5 | OrthoTwin3D est direct; Haiku 5.5 choisit deux transferts primaires cohérents et reconnaît le manque de fusion de capteurs. |
| 22 | 7980 — Data Engineer - Toulouse (H/F) | 2 (M1) | 3 (m1) | 4 (m1) | GPT-5.4 mini | Élan est le projet de pipeline le plus utile; GPT-5.4 mini couvre Spark, AWS, Python et SQL avec sobriété. |
| 23 | 7523 — Consultant data | 2 (M1) | 3 (m1) | 3 (m1) | Haiku 5.5, GPT-5.4 mini | Aucun projet ne démontre Power BI ou Azure; GPT-5.4 mini et Haiku 5.5 sont deux transferts acceptables sans avantage net. |
| 24 | 8610 — Forward Deployed Engineer F/H Paris h/f | 4 (—) | 3 (m1) | 2 (M1) | GPT-5.4 | Même trio pertinent; GPT-5.4 exprime clairement l'absence d'expérience forward deployed documentée. |
| 25 | 11200 — Data Scientist pour pièces de rechange (F/H/Nb) 1 | 4 (m1) | 4 (m1) | 2 (M1) | GPT-5.4, Haiku 5.5 | Activation Bot Traffic acceptable car anomaly detection est explicite; Haiku 5.5 et GPT-5.4 restent raisonnables, sans preuve directe de prévision de pièces. |
| 26 | 9936 — Ingénieur IA Cloud & MLOps (H/F) | 3 (m1) | 4 (m1) | 2 (M1) | Haiku 5.5 | Haiku 5.5 privilégie Docker et CI/CD et signale exactement les manques GCP/Kubernetes. |
| 27 | 9551 — Ingénieur IA -Java/Angular | 4 (—) | 3 (m1) | 3 (m1) | GPT-5.4 | Trois projets directs et deux certifications pertinentes chez tous; GPT-5.4 garde les limites fullstack et seniorité explicites. |
| 28 | 6658 — Proxy Product Owner GenAI – Agentic AI & Omnichannel (H/F) | 4 (m1) | 4 (—) | 3 (m1) | GPT-5.4, Haiku 5.5 | Même trio fort; Haiku 5.5 et GPT-5.4 reconnaissent la distance entre agent de pentest et plateforme de relation client. |
| 29 | 3788 — Ingénieur(e) R&D / Robotique - IA - Industrialisation (H/F) | 2 (M1) | 4 (—) | 3 (m1) | Haiku 5.5 | OrthoTwin3D puis SVC et Élan fournissent vision, pipeline multimodal et produit, sans inventer la robotique. |
| 30 | 4309 — Data Engineer Snowflake F/H | 1 (M1) | 3 (m1) | 4 (—) | GPT-5.4 mini | Même trio de transferts; GPT-5.4 mini est le plus ciblé sur SQL, Python, Spark, pipelines et lacunes Snowflake. |
| 31 | 8309 — Concepteur / Développeur BI & Analyste Data H/F | 2 (M1) | 4 (—) | 4 (m1) | Haiku 5.5, GPT-5.4 mini | Aucun projet ne démontre ODI/SAP BI; GPT-5.4 mini et Haiku 5.5 gardent les outils absents explicites et des compétences ciblées. |
| 32 | 11727 — Ingénieur Développeur Logiciel (H/F) | 2 (M1) | 4 (—) | 3 (m1) | Haiku 5.5 | Trois projets AI pertinents; Haiku 5.5 qualifie correctement la distance fullstack et les deux années d'expérience demandées. |
| 33 | 5733 — Backend Engineer / Data Scientist | 2 (M1, m1) | 4 (—) | 3 (m1) | Haiku 5.5 | Même trio primaire défendable; Haiku 5.5 cadre précisément les compétences absentes du backend cible. |
| 34 | 8380 — Data Scientist - GCP | 0 (C1, M1, m1) | 4 (—) | 2 (M1) | Haiku 5.5 | Haiku 5.5 cible la modélisation demande, la loss et l'évaluation sans confondre drift et condition d'activation bot. |
| 35 | 9095 — Data scientist / ingénieur ia expérimenté (h/f) (H/F) | 1 (M1, m1) | 3 (m1) | 3 (m1) | Haiku 5.5, GPT-5.4 mini | Même trio principal cohérent; GPT-5.4 mini/Haiku 5.5 gardent une sélection utilisable, GPT-5.4 dilue les compétences clés. |
| 36 | 10191 — Développeur / Ingénieur IA - LLM & Agents IA - Assurance (F/H) | 3 (m1) | 4 (—) | 3 (m1) | Haiku 5.5 | Même trio direct; Haiku 5.5 formule les lacunes enterprise et sécurité sans surpromesse. |
| 37 | 7578 — Ingénieur GNC – Guidage, Navigation & Contrôle F/H | 4 (m1) | 2 (M1) | 0 (C1, M2) | GPT-5.4 | OrthoTwin3D est le meilleur transfert; GPT-5.4 couvre signal/math sans revendiquer contrôle robotique. |
| 38 | 7825 — Ingénieur de Données GCP-(h/f) | 2 (M1) | 4 (—) | 4 (m1) | Haiku 5.5, GPT-5.4 mini | Même trio de transferts; Haiku 5.5/GPT-5.4 mini reconnaissent la lacune GCP et sélectionnent SQL/Python/Spark, sans prétendre BigQuery. |
| 39 | 11825 — Data Analyst Confirmé F/H | 1 (M1) | 3 (m1) | 4 (m1) | GPT-5.4 mini | Même trio direct; GPT-5.4 mini hiérarchise données et assistants, sans changer excessivement le titre candidat. |
| 40 | 9567 — Ingénieur·e / Consultant·e HPC, GPU, Cloud & IA H/F | 1 (M1) | 4 (—) | 3 (m1) | Haiku 5.5 | Haiku 5.5 choisit entraînement, produit et orchestration, et limite explicitement DDP aux compétences déclarées sans projet multi-GPU. |

## Recommandation

Sur ce jeu de 40 offres, utiliser Haiku 5.5 high comme modèle de référence opérationnelle pour `CVSelection`, avec validation applicative des politiques de projet et du catalogue de compétences. Il a le meilleur résultat métier observé et le coût estimé le plus faible. Garder GPT-5.4 mini high comme second candidat seulement si un besoin particulier le justifie : ses six victoires isolées ne compensent pas ici deux défauts critiques et une dépense estimée plus élevée. Ne pas retenir GPT-5.4 none comme choix par défaut sur ce corpus en raison de quatre violations critiques et d’une fréquence élevée de compétences hors sujet. Avant généralisation, refaire l’évaluation avec au moins un second évaluateur aveugle et un autre échantillon d’offres ; les présents résultats ne mesurent ni le CV rendu ni un succès de candidature.
