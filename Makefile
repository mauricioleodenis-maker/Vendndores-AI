PY ?= .venv/bin/python
.PHONY: check dev worker test lint fmt audit migrate seed create-owner up down logs backup

dev:
	$(PY) -m uvicorn app.main:create_app --factory --reload --port 8000
worker:
	$(PY) -m arq app.worker.WorkerSettings
test:
	$(PY) -m pytest --cov=app --cov-report=term-missing
lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check app tests alembic
fmt:
	$(PY) -m ruff check --fix . && $(PY) -m ruff format app tests alembic
audit:
	$(PY) -m pip_audit
	$(PY) -m bandit -q -r app -ll
check: lint audit test
migrate:
	$(PY) -m alembic upgrade head
seed:
	$(PY) -m app.cli seed
create-owner:
	$(PY) -m app.cli create-owner
up:
	docker compose up -d --build
down:
	docker compose down
logs:
	docker compose logs -f web worker
backup:
	sh deploy/backup.sh
