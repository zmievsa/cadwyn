SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

install:
	uv sync --all-extras --dev

lint:
	pre-commit run --all-files

test:
	uv run --with tox --with tox-uv tox
