# Makefile for managing the ambient-expense-agent project

.PHONY: install playground test clean generate-traces grade

install:
	@echo "Installing dependencies..."
	agents-cli install

playground:
	@echo "Launching the Ambient Agent local service..."
	uv run uvicorn expense_agent.fast_api_app:app --host 0.0.0.0 --port 8080

test:
	@echo "Running tests..."
	uv run pytest

generate-traces:
	@echo "Generating traces..."
	uv run python tests/eval/generate_traces.py

grade:
	@echo "Grading traces..."
	uv run agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml

clean:
	@echo "Cleaning up local cache and virtual environment..."
	rm -rf .pytest_cache .venv

