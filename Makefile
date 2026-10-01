.PHONY: install lint type test check

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .

type:
	mypy

test:
	pytest

check: lint type test
