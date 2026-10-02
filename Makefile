.PHONY: install lint type test e2e check build

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .

type:
	mypy

test:
	pytest

e2e:
	python scripts/e2e_check.py

build:
	python -m build --wheel --no-isolation

check: lint type test
