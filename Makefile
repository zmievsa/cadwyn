SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

install:
	uv sync --all-extras --dev

lint:
	pre-commit run --all-files

test:
	rm -r coverage; \
	uv run coverage run --source=. -m pytest .; \
	uv run coverage combine; \
	uv run coverage report --fail-under=100 --show-missing;
