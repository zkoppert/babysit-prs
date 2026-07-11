.PHONY: test lint format clean install-test

install-test:
	python3 -m pip install -r requirements-test.txt

test:
	python3 -m pytest -v --cov=babysit_prs --cov-config=.coveragerc --cov-fail-under=90 --cov-report term-missing

lint:
	# stop the build if there are Python syntax errors or undefined names
	python3 -m flake8 . --config=.github/linters/.flake8 --count --select=E9,F63,F7,F82 --show-source
	python3 -m flake8 . --config=.github/linters/.flake8 --count --exit-zero --max-complexity=15 --max-line-length=150
	python3 -m isort --check-only --settings-file=.github/linters/.isort.cfg .
	python3 -m pylint --rcfile=.github/linters/.python-lint --fail-under=9.0 *.py
	python3 -m mypy --config-file=.github/linters/.mypy.ini babysit_prs.py
	python3 -m black --check .

format:
	python3 -m isort --settings-file=.github/linters/.isort.cfg .
	python3 -m black .

clean:
	rm -rf .pytest_cache .coverage __pycache__ .mypy_cache
