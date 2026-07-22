SHELL := /bin/bash
py_warn = PYTHONDEVMODE=1
.DEFAULT_GOAL := help

.PHONY: help install hooks lint check check-tools check-prek

help: ## Show the available developer commands
	@awk 'BEGIN {FS = ":.*##"; printf "Usage:\n  make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*?##/ {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

define check-command
if ! command -v $(1) >/dev/null 2>&1; then \
		echo "Required command '$(1)' was not found."; \
		echo "Install it with:"; \
		echo "  $(2)"; \
		status=127; \
fi
endef

define check-tox-runner
if command -v tox >/dev/null 2>&1 && ! tox config -e py3.10 >/dev/null 2>&1; then \
		echo "Required tox runner 'uv-venv-lock-runner' was not found."; \
		echo "Install it with:"; \
		echo "  uv tool install tox --with tox-uv"; \
		status=127; \
fi
endef

install: ## Install the project and standalone developer tools
	uv sync --all-extras --dev
	uv tool install prek
	uv tool install tox --with tox-uv

hooks: check-prek ## Install the prek git hooks
	prek install -f

lint: check-prek ## Run all prek hooks
	prek run --all-files

check: check-tools ## Run the full local CI-equivalent suite
	tox run-parallel --parallel-no-spinner

check-tools:
	@status=0; \
	$(call check-command,prek,uv tool install prek); \
	$(call check-command,tox,uv tool install tox --with tox-uv); \
	$(call check-tox-runner); \
	exit $$status

check-prek:
	@status=0; \
	$(call check-command,prek,uv tool install prek); \
	exit $$status
