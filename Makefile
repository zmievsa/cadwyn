SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

install:
	poetry install --all-extras

lint:
	pre-commit run --all-files

format:
	poetry run ruff check . --fix && poetry run ruff format .;

test:
	rm -f .coverage coverage.xml; \
	poetry run pytest tests \
		--cov=. \
		--cov-report=term-missing:skip-covered \
		--cov-branch \
		--cov-append \
		--cov-report=xml \
		--cov-fail-under=100;
