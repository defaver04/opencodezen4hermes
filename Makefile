# opencodezen4hermes — development entry points.
#
# Everything here runs on a bare python3.12 with no third-party packages; the
# test target is exactly the command docs/BLUEPRINT.md §8 promises. Targets that
# talk to the network are opt-in and never part of `make test`.

PYTHON ?= python3
PORT   ?= 8799

.DEFAULT_GOAL := help
.PHONY: help test lint doctor verify sidecar seed clean

help: ## List the available targets
	@echo "opencodezen4hermes — make targets:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-9s %s\n", $$1, $$2}'
	@echo ""
	@echo "  test    stdlib unittest only, no network (BLUEPRINT §8)"
	@echo "  lint    byte-compile every python tree in the repo"
	@echo ""

test: ## Run the standard-library unittest suite (no network)
	$(PYTHON) -m unittest discover -s tests -v

lint: ## Byte-compile sidecar, install, scripts, tests and plugin
	$(PYTHON) -m compileall -q sidecar install scripts tests plugin

doctor: ## Diagnose this machine (sidecar, plugin, config seed, pack layout)
	$(PYTHON) scripts/doctor.py

verify: ## Live proof against the real Zen lane (needs network)
	$(PYTHON) scripts/verify_live.py

sidecar: ## Run the sidecar in the foreground on the dev port, verbose
	$(PYTHON) -m sidecar --verbose --port $(PORT)

seed: ## Write the providers.opencode-zen-free route into the Hermes config
	$(PYTHON) install/seed_config.py

clean: ## Remove __pycache__ trees and test artefacts
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .coverage htmlcov build dist *.egg-info
	@echo "cleaned"
