SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1

.PHONY: install lint check test check-tools check-prek check-tox

define check-command
if ! command -v $(1) >/dev/null 2>&1; then \
		echo "Required command '$(1)' was not found."; \
		echo "Install it with:"; \
		echo "  $(2)"; \
		status=127; \
fi
endef

install:
	uv sync --all-extras --dev

lint: check-prek
	prek run --all-files

check: check-tools
	tox run-parallel --parallel-no-spinner

test: check-tools
	tox run-parallel --parallel-no-spinner

check-tools:
	@status=0; \
	$(call check-command,prek,uv tool install prek); \
	$(call check-command,tox,uv tool install tox); \
	exit $$status

check-prek:
	@status=0; \
	$(call check-command,prek,uv tool install prek); \
	exit $$status

check-tox:
	@status=0; \
	$(call check-command,tox,uv tool install tox); \
	exit $$status
