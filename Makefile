SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

ifndef FAIL_UNDER
    export FAIL_UNDER=80
endif

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
		--cov-fail-under=${FAIL_UNDER};

ci_supertest:
	poetry run pip install 'pydantic==1.10.13' && \
	make test && \
	poetry run pip install 'pydantic==2.5.3' && \
	make test FAIL_UNDER=100;

supertest:
	poetry install --all-extras
	make ci_supertest && \
	poetry run pip install 'pydantic==2.5.3' && \
	rm coverage.xml .coverage;
