SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

install:
	poetry install --all-extras

lint:
	pre-commit run --all-files

format:
	poetry run ruff . --fix && poetry run ruff format .;

test:
	poetry run pytest tests \
		--cov=. \
		--cov-report=term-missing:skip-covered \
		--cov-branch \
		--cov-append \
		--cov-report=xml \
		--cov-fail-under=100;
