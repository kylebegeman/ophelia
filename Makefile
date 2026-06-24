PYTHON ?= .venv/bin/python

.PHONY: venv validate-examples render-examples validate-manifests render-manifests validate-fixtures validate-adoption-fixtures validate-fixture-plugins live-readiness-fixtures live-drills-fixtures live-hydration-reviewed-fixture lumen-console-fixtures production-hardening-fixtures open-source-audit open-source-audit-strict test compile docs-check

venv:
	python3 -m venv .venv
	.venv/bin/python -m ensurepip --upgrade
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[test]"

validate-examples:
	./cli/ship validate examples/service-app.ophelia.yml
	./cli/ship validate examples/multi-service-app.ophelia.yml
	./cli/ship validate examples/portfolio.ophelia.yml
	./cli/ship validate examples/static-site.ophelia.yml
	./cli/ship validate examples/redirect-host.ophelia.yml
	./cli/ship validate examples/tunnel-app.ophelia.yml

render-examples:
	./cli/ship render examples/service-app.ophelia.yml --output-dir build/service-app
	./cli/ship render examples/multi-service-app.ophelia.yml --output-dir build/multi-service-app
	./cli/ship render examples/portfolio.ophelia.yml --output-dir build/portfolio
	./cli/ship render examples/static-site.ophelia.yml --output-dir build/static-site
	./cli/ship render examples/redirect-host.ophelia.yml --output-dir build/redirect-host
	./cli/ship render examples/tunnel-app.ophelia.yml --output-dir build/tunnel-app

validate-manifests:
	for manifest in manifests/*.ophelia.yml; do ./cli/ship validate "$$manifest" || exit 1; done

render-manifests:
	for manifest in manifests/*.ophelia.yml; do app=$$(basename "$$manifest" .ophelia.yml); ./cli/ship render "$$manifest" --output-dir "build/$$app" || exit 1; done

validate-fixtures:
	for manifest in fixtures/app-suite/manifests/*.ophelia.yml; do ./cli/ship validate "$$manifest" || exit 1; done

validate-adoption-fixtures:
	@for repo in fixtures/adoption/*; do \
		if [ -d "$$repo" ]; then \
			app=$$(basename "$$repo"); \
			./cli/ship app adoption plan "$$app" --repo-path "$$repo" --environment staging --json >/dev/null || exit 1; \
		fi; \
	done

validate-fixture-plugins:
	@./cli/ship plugins catalog --plugins-dir fixtures/app-suite/plugins --json >/dev/null

live-readiness-fixtures:
	@./cli/ship live-readiness run --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml --provider-config fixtures/app-suite/integrations.yml --allow-blocked --json

live-drills-fixtures:
	@./cli/ship live-drills run-all --profiles fixtures/app-suite/live-drills.yml --json

live-hydration-reviewed-fixture:
	@./cli/ship live-hydration validate-evidence --profile fixture-postgres-focused --profiles fixtures/app-suite/live-drills.yml --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging --json >/dev/null
	@./cli/ship live-hydration promotion-plan --profile fixture-postgres-focused --profiles fixtures/app-suite/live-drills.yml --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging --json

lumen-console-fixtures:
	@./cli/ship lumen console-data --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --plugins-dir fixtures/app-suite/plugins --json

production-hardening-fixtures:
	@./cli/ship hardening production-readiness --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml --provider-config fixtures/app-suite/integrations.yml --plugins-dir fixtures/app-suite/plugins --include-fixture-suite --allow-blocked-live-readiness --json

open-source-audit:
	@./cli/ship open-source audit --allow-blocked --json

open-source-audit-strict:
	@./cli/ship open-source audit --fail-on-warnings --json

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

compile:
	$(PYTHON) -m compileall src

docs-check:
	PYTHONPATH=src $(PYTHON) -m ophelia.docs_check
