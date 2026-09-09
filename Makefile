.PHONY: help install install-desktop venv check test test-fast lint format clean run-desktop build run-cli init-db refresh-embeddings sync-profile check-profile-sync

PY ?= python3
VENV = .venv
BIN = $(VENV)/bin
DIRECTION ?= to-app
COMPANIES_CSV ?= $(CURDIR)/data/company-targeting/companies.csv

.PHONY: sync-companies check-companies-sync

help:
	@echo "Élan — commandes disponibles"
	@echo ""
	@echo "  make venv             Cree un venv local"
	@echo "  make install          Installe le coeur et les outils de développement"
	@echo "  make install-desktop  Installe l'application macOS et les outils de build"
	@echo "  make init-db          Initialise la base SQLite"
	@echo "  make refresh-embeddings  Pre-calcule les embeddings du profil et des projets"
	@echo "  make check-profile-sync Compare le profil du depot avec celui d'Elan"
	@echo "  make sync-profile     Copie le profil vers Elan avec sauvegarde (DIRECTION=from-app pour l'inverse)"
	@echo "  make check-companies-sync Aperçu CSV vers base Élan, sans écriture"
	@echo "  make sync-companies   Synchronise le CSV de référence vers Élan, préserve les fiches checked"
	@echo "  make check            Verifie le code et lance les tests rapides"
	@echo "  make test             Lance les tests"
	@echo "  make test-fast        Tests rapides (skip integration)"
	@echo "  make lint             Verifie le code"
	@echo "  make format           Formate le code"
	@echo "  make run-desktop      Lance l'application macOS en développement"
	@echo "  make build            Construit dist/Elan.app"
	@echo "  make run-cli          Lance la CLI de maintenance (elan --help)"
	@echo "  make clean            Supprime caches et builds"

venv:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip wheel setuptools

install: venv
	$(BIN)/pip install -e ".[dev]"

install-desktop: venv
	$(BIN)/pip install -e ".[desktop,pdf,dev]"

init-db:
	$(BIN)/elan init-db

refresh-embeddings:
	$(BIN)/elan refresh-embeddings

check-profile-sync:
	$(BIN)/elan sync-profile --project-profile "$(CURDIR)/smartapply/profile/data" --direction "$(DIRECTION)" --dry-run

sync-profile:
	$(BIN)/elan sync-profile --project-profile "$(CURDIR)/smartapply/profile/data" --direction "$(DIRECTION)"

check-companies-sync:
	$(BIN)/elan sync-companies --csv "$(COMPANIES_CSV)" --dry-run

sync-companies:
	$(BIN)/elan sync-companies --csv "$(COMPANIES_CSV)"

check: lint test-fast

test:
	$(BIN)/pytest

test-fast:
	$(BIN)/pytest -m "not integration and not llm"

lint:
	$(BIN)/ruff check smartapply tests tools/desktop_visual_check.py

format:
	$(BIN)/ruff format smartapply tests tools/desktop_visual_check.py
	$(BIN)/ruff check --fix smartapply tests tools/desktop_visual_check.py

run-desktop:
	$(BIN)/elan-desktop

build:
	$(BIN)/python -m smartapply.desktop.build_macos

run-cli:
	$(BIN)/elan --help

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info htmlcov .coverage
