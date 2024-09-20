SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

install:
	poetry install --all-extras

lint:
	pre-commit run --all-files

test:
	rm -r coverage; \
	poetry run coverage run --source=. -m pytest .; \
	coverage combine; \
	coverage report --fail-under=100;
