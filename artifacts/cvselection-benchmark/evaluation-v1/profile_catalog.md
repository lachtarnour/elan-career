# Candidate profile used for the blind review

Candidate title: Data Scientist - Applied AI, NLP & Multimodal ML

## Projects

### proj_svc — Singing Voice Conversion (primary)

Status: Ongoing. Activation: none.

- Convert a singing voice into a target singer’s voice. An adapted SoftVC model, trained with PyTorch on M4Singer, uses HuBERT-Soft for content and RMVPE for pitch, with HiFi-GAN handling audio synthesis.

### proj_scifact_rag — SciFact RAG Verifier (primary)

Status: Ongoing. Activation: none.

- Check scientific claims and generate cited answers using hybrid RAG combining BM25, SPECTER2/FAISS, RRF fusion and a local or API-based LLM. Document retrieval Recall@5: 0.7812 on SciFact.

### proj_math_consistent_qwen — Math-Consistent Qwen (primary)

Status: Ongoing research project. Activation: none.

- Improve the reliability of Qwen’s mathematical answers by supervising intermediate calculations. LoRA fine-tuning on GSM8K, extraction of calculation steps, construction of executable graphs, and development of custom training losses penalizing discrepancies between predicted and recomputed results.

### proj_smartapply — Élan (primary)

Status: Completed. Activation: none.

- Automate job discovery and the creation of tailored CVs and cover letters through an AI pipeline delivered as a standalone native macOS app: multi-source ingestion, semantic matching, structured LLM generation and consistency checks against the candidate’s profile.

### orthogeo3d — OrthoTwin3D (primary)

Status: Ongoing research project. Activation: none.

- Identify and number teeth in 3D scans by segmenting point clouds with DGCNN, then reconstruct full-scan segmentation through multi-view voting. mIoU: 0.6682 on Teeth3DS.

### proj_bot_traffic_anomaly — Bot Traffic Detection (specialized)

Status: Completed. Activation: Détection de bots ou trafic automatisé explicitement demandée, Détection d’anomalies, fraude ou abus explicitement demandée.

- Identify suspicious web traffic and prioritize events for investigation in heterogeneous logs using anomaly detection with Isolation Forest and RRCF.

### proj_aal_stock_forecasting — AAL Stock Forecasting (disabled)

Status: Completed. Activation: none.

- Forecast American Airlines Group stock prices using ARIMA/SARIMA models in R, selected through statistical criteria and residual analysis.

### proj_pix2pix_maps — Pix2Pix Maps (specialized)

Status: Completed. Activation: Traduction image-à-image explicitement demandée, Génération conditionnelle d’images ou modèles GAN explicitement demandés.

- Generate map-style images from satellite imagery with Pix2Pix, pairing a U-Net generator with a PatchGAN discriminator. PSNR: 23.72 dB on the Maps dataset.

### proj_ner_camembert — BERT NER on CoNLL-2003 (specialized)

Status: Completed. Activation: Reconnaissance ou extraction d’entités nommées explicitement demandée, Token classification ou étiquetage de séquences explicitement demandé.

- Recognize named entities (NER) in text by fine-tuning bert-base on the CoNLL-2003 dataset. F1 score: 93.98%.

### proj_rl_gym — Tabular Reinforcement Learning (disabled)

Status: Completed. Activation: none.

- Train agents to solve games in predefined OpenAI Gym environments by implementing the tabular reinforcement learning algorithms SARSA and Q-learning.

### proj_autonomous_pentest_agent — Pentesting Agent (primary)

Status: In development. Activation: none.

- Automate web security testing by orchestrating Planner-Worker LLM agents with LangGraph against Docker-isolated targets. Solved 8 of the 10 XBEN challenges tested.

## Certificates

- cert_rag_dlai: Retrieval Augmented Generation (RAG) — Weaviate, vector search, retrieval and LLM evaluation.
- cert_foundation_langchain: Foundation LangChain — Introduction to LangChain, DeepAgents and MCP.

## Allowed skills

- ml_ai: PyTorch, TensorFlow, scikit-learn, Tree-based models & boosting, NLP, Transformers, Hugging Face Transformers, RNN, Time-series modeling, Anomaly detection, GNN, Fine-tuning, Distributed training (PyTorch DDP)
- generative_agentic_ai: LangChain, LangGraph, RAG, Vector search, FAISS
- knowledge_graphs: RDF, SPARQL, Neo4j
- computer_vision: CNNs, torchvision, OpenCV, Image processing, Segmentation, Object detection, Facial affect estimation
- speech_audio: Speech processing, Speech-to-text, Speaker diarization, openSMILE, Pyannote
- data_analysis: Python, SQL, R, pandas, NumPy, SciPy, Polars, Statistical analysis, Forecasting, Streamlit / Panel / Plotly
- data_infra: Git, Docker, FastAPI, Flask, AWS, OVHcloud, Spark, CI/CD, Grafana, Model quantization, Weights & Biases
- stats_signal: ARIMA/SARIMA, Stationarity tests, Monte Carlo methods, Mathematical optimization, Markov chains, Data imputation
- rl: SARSA, Q-learning, Policy gradients, MCTS
