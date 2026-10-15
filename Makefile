.PHONY: test lint demo up down

test:
	uv run pytest

lint:
	uv run ruff check .

demo:
	uv run python -m noticedelta.demo

up:
	docker compose up -d

down:
	docker compose down
