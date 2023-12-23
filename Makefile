SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1


install:
	poetry install --all-extras

lint:
	pre-commit run --all-files

format:
	poetry run ruff . --fix; \
	poetry run ruff format .;

test:
	poetry run pytest --cov=. --cov-report=term-missing:skip-covered --cov-branch --cov-append --cov-report=xml tests;

ci_supertest:
	poetry add 'pydantic@^1.0.0' || exit 1; \
	make test || exit 1; \
	poetry add 'pydantic@^2.0.0' || exit 1; \
	make test; \

supertest:
	make ci_supertest; \
	poetry add 'pydantic@>=1.0.0'; \
	rm coverage.xml .coverage;
