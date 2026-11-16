.PHONY: test lint demo eval up down

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

eval:
	uv run python scripts/eval.py
