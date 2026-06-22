PYTHON ?= .venv/bin/python

.PHONY: venv validate-examples render-examples validate-manifests render-manifests validate-fixtures live-readiness-fixtures test compile docs-check

venv:
	python3 -m venv .venv
	.venv/bin/python -m ensurepip --upgrade
	.venv/bin/python -m pip install PyYAML

validate-examples:
	./cli/ship validate examples/dragonwriter.ophelia.yml
	./cli/ship validate examples/pokedex.ophelia.yml
	./cli/ship validate examples/portfolio.ophelia.yml
	./cli/ship validate examples/aspectavy-staging.ophelia.yml
	./cli/ship validate examples/aspectavy-production-mirror.ophelia.yml

render-examples:
	./cli/ship render examples/dragonwriter.ophelia.yml --output-dir build/dragonwriter
	./cli/ship render examples/pokedex.ophelia.yml --output-dir build/pokedex
	./cli/ship render examples/portfolio.ophelia.yml --output-dir build/portfolio
	./cli/ship render examples/aspectavy-staging.ophelia.yml --output-dir build/aspectavy-staging
	./cli/ship render examples/aspectavy-production-mirror.ophelia.yml --output-dir build/aspectavy-production-mirror

validate-manifests:
	for manifest in manifests/*.ophelia.yml; do ./cli/ship validate "$$manifest" || exit 1; done

render-manifests:
	for manifest in manifests/*.ophelia.yml; do app=$$(basename "$$manifest" .ophelia.yml); ./cli/ship render "$$manifest" --output-dir "build/$$app" || exit 1; done

validate-fixtures:
	for manifest in fixtures/app-suite/manifests/*.ophelia.yml; do ./cli/ship validate "$$manifest" || exit 1; done

live-readiness-fixtures:
	./cli/ship live-readiness run --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml --provider-config fixtures/app-suite/integrations.yml --allow-blocked --json

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

compile:
	$(PYTHON) -m compileall src

docs-check:
	PYTHONPATH=src $(PYTHON) -m ophelia.docs_check
