.PHONY: install test api-docs hygiene frontend docker-check verify

install:
	python3.12 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt
	cd web && npm ci

test:
	PYTHONPATH=. .venv/bin/python -m pytest

api-docs:
	PYTHONPATH=. .venv/bin/python scripts/check_api_docs.py

hygiene:
	PYTHONPATH=. .venv/bin/python scripts/check_repository_hygiene.py

frontend:
	cd web && npm run build

docker-check:
	docker compose config --quiet
	docker build -t aioption:local .

verify: test api-docs hygiene frontend
