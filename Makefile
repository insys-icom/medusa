MYPY := mypy
PYTEST := pytest
RUFF := ruff


check:
	${MYPY} src tests
	${RUFF} check src tests

fix:
	${MYPY} src tests
	${RUFF} check --fix src tests

format:
	${RUFF} format src tests

test:
	${PYTEST}


.PHONY: check fix format test
