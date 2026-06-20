# Convenience targets for the windmill-operator repo. Per-charm tooling
# (tox/uv) is the source of truth; this just wraps the common workflows.

CHARMS := windmill windmill-worker

.PHONY: help sync format lint typecheck unit test build clean
help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

sync:           ## Install dependencies for all charms
	@for c in $(CHARMS); do echo "== $$c =="; (cd charms/$$c && uv sync); done

format:         ## Auto-format source
	@for c in $(CHARMS); do echo "== $$c =="; (cd charms/$$c && uv run --group lint ruff format src tests); done

lint:           ## Lint (ruff + codespell)
	@for c in $(CHARMS); do echo "== $$c =="; (cd charms/$$c && uv run --group lint ruff check src tests && uv run --group lint codespell .); done

typecheck:      ## Static type checks (pyright)
	@for c in $(CHARMS); do echo "== $$c =="; (cd charms/$$c && uv run --group lint pyright); done

unit:           ## Run unit tests for all charms
	@for c in $(CHARMS); do echo "== $$c =="; (cd charms/$$c && PYTHONPATH=src:lib uv run --group unit pytest tests/unit -v); done

test: lint typecheck unit  ## Run all checks

build:          ## Pack both charms
	@for c in $(CHARMS); do echo "== $$c =="; (cd charms/$$c && charmcraft pack); done

clean:          ## Remove build artefacts and caches
	@for c in $(CHARMS); do echo "== $$c =="; (cd charms/$$c && rm -rf .coverage .pytest_cache .ruff_cache .mypy_cache build *.charm); done
