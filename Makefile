SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1


.DEFAULT_GOAL := pre-commit


pre-commit:
	pre-commit run --all-files

format:
	poetry run ruff . --fix; \
	poetry run black .;

test:
	poetry run pytest --cov=. --cov-report=term-missing:skip-covered --cov-branch --cov-report=xml tests;
