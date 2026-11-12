.PHONY: bootstrap provision-memory test test-unit test-live start stop status
bootstrap:
	uv run research-agent bootstrap
provision-memory:
	uv run research-agent provision-memory
test:
	uv run pytest -m 'not live'
test-unit:
	uv run pytest tests/unit -q
test-live:
	RA_ALLOW_LIVE_TESTS=true uv run pytest -m live
start:
	docker compose up -d
stop:
	docker compose stop
status:
	docker compose ps
