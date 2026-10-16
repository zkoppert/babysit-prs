# Contributing

Thanks for your interest in improving `babysit-prs`.

## Development setup

```bash
git clone https://github.com/zkoppert/babysit-prs.git
cd babysit-prs
make install-test
```

The tool itself has no third-party runtime dependencies (Python 3.11+ standard
library only). The test and lint tools are in `requirements-test.txt`.

## Before opening a pull request

```bash
make format # apply isort + black
make lint    # checks black, isort, flake8, pylint (>=9.0), mypy
make test    # pytest with coverage (>=90%)
```

`make lint` checks code without changing it; run `make format` to apply formatting.

Please add or update tests for any behavior change. The decision logic in
`classify()` is pure and fully unit-tested; keep it that way by pushing side
effects (gh calls, notifications) to the edges.

## Guidelines

- Keep the [guardrails](README.md#guardrails) intact.
- Prefer small, single-purpose pull requests with a clear "why".
- Comments should explain *why*, not restate *what* the code does.
