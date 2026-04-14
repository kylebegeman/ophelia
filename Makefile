.PHONY: venv validate-examples render-examples test

venv:
	python3 -m venv .venv
	.venv/bin/python -m ensurepip --upgrade
	.venv/bin/python -m pip install PyYAML

validate-examples:
	./cli/ship validate examples/dragonwriter.ophelia.yml
	./cli/ship validate examples/pokedex.ophelia.yml
	./cli/ship validate examples/portfolio.ophelia.yml
	./cli/ship validate examples/aspectavy-staging.ophelia.yml

render-examples:
	./cli/ship render examples/dragonwriter.ophelia.yml --output-dir build/dragonwriter
	./cli/ship render examples/pokedex.ophelia.yml --output-dir build/pokedex
	./cli/ship render examples/portfolio.ophelia.yml --output-dir build/portfolio
	./cli/ship render examples/aspectavy-staging.ophelia.yml --output-dir build/aspectavy-staging

test:
	PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v

compile:
	.venv/bin/python -m compileall src
