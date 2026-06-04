# model-sherpa plugin — dev tooling
#
# Targets:
#   make lint      — run pyflakes on the plugin source
#   make test      — run the pytest smoke suite
#   make check     — both, in order; exits non-zero on any failure
#   make benchmark — generate performance metrics from local state
#   make clean     — remove cached .pyc / __pycache__ / .pytest_cache

PLUGIN := __init__.py
TESTS  := tests/

.PHONY: lint test check benchmark clean

lint:
	@command -v pyflakes >/dev/null 2>&1 || { \
		echo "pyflakes not installed. Install with:"; \
		echo "  python3 -m pip install --user pyflakes"; \
		exit 2; \
	}
	pyflakes $(PLUGIN)

test:
	python3 -m pytest $(TESTS) -v

check: lint test

benchmark:
	python3 benchmark.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache
