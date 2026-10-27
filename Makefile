PY := .venv/bin/python
SERVICE ?= orders-service

.PHONY: setup test test-states dev stage build check clean

## setup: create .venv with the tools the scripts and tests need
setup:
	python3 -m venv .venv
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements-dev.txt

## test: run the service's tests
test:
	$(PY) -m pytest -q -p no:cacheprovider services/$(SERVICE)

## test-states: run every demo state's tests (the broken-test variant is meant to fail)
test-states:
	@for dir in demo-states/$(SERVICE)/*/; do \
		printf '%-48s' "$$dir"; $(PY) -m pytest -q -p no:cacheprovider "$$dir" | tail -1; \
	done

## dev: staging on :8001 and production on :8002 (STATE=1.4.1 serves a demo state)
dev:
	$(PY) scripts/dev.py $(SERVICE) $(if $(STATE),--state $(STATE))

## stage: copy a demo state into the service folder, no commit (V=1.4.0)
stage:
	$(PY) scripts/stage.py $(SERVICE) $(V) --no-commit

## build: package the service into dist/
build:
	$(PY) scripts/build.py $(SERVICE)

## check: run the pipeline's check against the local runner (ENV=production V=1.4.0)
check:
	$(PY) scripts/check.py $(SERVICE) $(ENV) --version $(V) --local

clean:
	rm -rf dist .local
