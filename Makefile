SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help setup backend-setup frontend-setup test lint fmt build demo serve doctor clean

help: ## Показати команди
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: backend-setup frontend-setup build ## Встановити залежності та зібрати UI

backend-setup: ## Python-залежності (uv)
	cd backend && uv sync --extra dev

frontend-setup: ## Node-залежності (React/Vite)
	cd frontend && npm install

test: ## Тести backend (офлайн)
	cd backend && uv run pytest -q

lint: ## ruff + TypeScript
	cd backend && uv run ruff check app tests
	cd frontend && npm run typecheck

fmt: ## Автовиправлення lint
	cd backend && uv run ruff check app tests --fix

build: ## Зібрати UI у frontend/dist
	cd frontend && npm run build

demo: build ## Офлайн-демо + жива студія на :8000
	cd backend && uv run daas demo --serve

serve: ## Запустити студію
	cd backend && uv run daas serve

doctor: ## Перевірити середовище
	cd backend && uv run daas doctor

clean: ## Прибрати білд-артефакти
	rm -rf frontend/dist backend/.pytest_cache backend/.ruff_cache
