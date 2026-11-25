.PHONY: up down logs test lint fmt typecheck migrate bootstrap smoke bench-aa diagrams dashboards

up:            ## build + start the stack (profiles from .env), wait for health
	docker compose up -d --build --wait
down:
	docker compose down
logs:
	docker compose logs -f --tail=100 api ingest worker embedder
test:
	uv run pytest
lint:
	uv run ruff check src tests sdk bench && uv run ruff format --check src tests
fmt:
	uv run ruff format src tests && uv run ruff check --fix src tests
typecheck:
	uv run mypy
migrate:
	docker compose run --rm migrate-postgres && docker compose run --rm migrate-clickhouse
bootstrap:     ## create a project + admin key (the key is printed once)
	curl -s -X POST localhost:8000/v1/admin/bootstrap -H "x-bootstrap-token: $${LUMEN_BOOTSTRAP_TOKEN:-change-me-bootstrap}" \
	  -H 'content-type: application/json' -d '{"project":"demo"}'
smoke:         ## end-to-end: bootstrap → OTLP → trace queryable
	bash scripts/smoke.sh
bench-aa:
	uv run python bench/aa_simulation.py
diagrams:
	python docs/diagrams/build_diagrams.py
dashboards:
	python deploy/grafana/build_dashboards.py
