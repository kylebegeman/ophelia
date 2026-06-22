PYTHON ?= .venv/bin/python

.PHONY: venv validate-examples render-examples validate-manifests render-manifests validate-fixtures validate-fixture-plugins live-readiness-fixtures live-drills-fixtures live-hydration-quark-staging live-hydration-scaffold-quark-staging live-hydration-validate-evidence-quark-staging live-hydration-probe-gate-quark-staging lumen-console-fixtures production-hardening-fixtures test compile docs-check

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

validate-fixture-plugins:
	@./cli/ship plugins catalog --plugins-dir fixtures/app-suite/plugins --json >/dev/null

live-readiness-fixtures:
	@./cli/ship live-readiness run --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml --provider-config fixtures/app-suite/integrations.yml --allow-blocked --json

live-drills-fixtures:
	@./cli/ship live-drills run-all --profiles fixtures/app-suite/live-drills.yml --json

live-hydration-quark-staging:
	@./cli/ship live-hydration report --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --allow-blocked --json

live-hydration-scaffold-quark-staging:
	@./cli/ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --json

live-hydration-validate-evidence-quark-staging:
	@tmp=$$(mktemp -d); \
	./cli/ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --output-dir "$$tmp/kit" --write --json >/dev/null; \
	./cli/ship live-hydration validate-evidence --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --input-dir "$$tmp/kit" --json

live-hydration-probe-gate-quark-staging:
	@./cli/ship live-hydration probe-gate --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --allow-blocked --json

lumen-console-fixtures:
	@./cli/ship lumen console-data --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --plugins-dir fixtures/app-suite/plugins --json

production-hardening-fixtures:
	@./cli/ship hardening production-readiness --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml --provider-config fixtures/app-suite/integrations.yml --plugins-dir fixtures/app-suite/plugins --include-fixture-suite --allow-blocked-live-readiness --json

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

compile:
	$(PYTHON) -m compileall src

docs-check:
	PYTHONPATH=src $(PYTHON) -m ophelia.docs_check
