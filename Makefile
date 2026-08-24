.PHONY: install test check
install:
	python3 -m pip install -e .
test:
	pytest -q
check:
	python3 -m compileall -q src
