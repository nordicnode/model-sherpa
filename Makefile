# model-sherpa plugin — dev tooling
#
# Targets:
#   make lint      — run ruff linter on the plugin source
#   make format    — auto-fix formatting with ruff
#   make typecheck — run mypy type checking
#   make test      — run the pytest smoke suite
#   make check     — all checks in order; exits non-zero on any failure
#   make benchmark — generate performance metrics from local state
#   make clean     — remove cached .pyc / __pycache__ / .pytest_cache

PLUGIN := __init__.py
TESTS  := tests/

.PHONY: lint format typecheck test check benchmark clean

lint:
	@command -v ruff >/dev/null 2>&1 || { \
		echo "ruff not installed. Install with:"; \
		echo "  pip install 'ruff>=0.4'"; \
		exit 2; \
	}
	ruff check $(PLUGIN) $(TESTS)

format:
	@command -v ruff >/dev/null 2>&1 || { \
		echo "ruff not installed. Install with:"; \
		echo "  pip install 'ruff>=0.4'"; \
		exit 2; \
	}
	ruff format $(PLUGIN) $(TESTS)

typecheck:
	@command -v mypy >/dev/null 2>&1 || { \
		echo "mypy not installed. Install with:"; \
		echo "  pip install 'mypy>=1.10'"; \
		exit 2; \
	}
	mypy $(PLUGIN)

test:
	python3 -m pytest $(TESTS) -v

check: lint typecheck test

benchmark:
	python3 benchmark.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache
