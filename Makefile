.PHONY: install sync-deps web test

install:
	uv venv .venv --python 3.13
	uv pip install -e ".[sync]"
	@test -f topics.yaml || cp topics.example.yaml topics.yaml
	@test -f .env || cp .env.example .env
	@echo "Activate with: source .venv/bin/activate"

sync-deps:
	uv pip install -e ".[sync]"

web:
	paperlab web serve

test:
	python -m compileall -q paperlab
	python -m unittest discover -s tests
