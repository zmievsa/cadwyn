SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1


.DEFAULT_GOAL := pre-commit


pre-commit:
	pre-commit run --all-files

format:
	poetry run ruff . --fix; \
	poetry run black .;

test:
	poetry run pytest --cov=. --cov-report=term-missing:skip-covered --cov-branch --cov-append --cov-report=xml tests;

supertest:
	poetry add 'pydantic@^1.0.0' || exit 1; \
	make test || exit 1; \
	poetry add 'pydantic@^2.0.0' || exit 1; \
	make test;  \
