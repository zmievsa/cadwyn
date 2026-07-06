SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

install:
	uv sync --all-extras --dev

lint:
	prek run --all-files

check:
	uv run tox run-parallel --parallel-no-spinner

test:
	uv run tox run-parallel --parallel-no-spinner
