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
	poetry add 'pydantic@^1.0.0' && \
	make test && \
	poetry add 'pydantic@^2.0.0' && \
	make test FAIL_UNDER=100;

supertest:
	make ci_supertest && \
	poetry add 'pydantic@>=1.0.0' && \
	rm coverage.xml .coverage;
