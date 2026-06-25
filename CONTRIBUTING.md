# Contributing to Model Sherpa

First off, thank you for considering contributing to Model Sherpa! It is people like you who make open source such a great community.

## Local Development Setup

To set up a local development environment:

1. **Fork and Clone**: Fork the repository on GitHub and clone it locally:
   ```bash
   git clone https://github.com/nordicnode/model-sherpa.git
   cd model-sherpa
   ```

2. **Dependencies**: Create a virtual environment and install the development dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate    # on Windows: venv\Scripts\activate
   pip install --upgrade pip
   pip install 'ruff>=0.4' 'mypy>=1.10' 'pytest>=8.0' 'pytest-asyncio>=0.23'
   ```
   (These match the `dev` extra in `pyproject.toml`: `pip install -e '.[dev]'`.)

## Development Workflow

We use a simple `Makefile` to enforce code quality and correctness:

- **Linting**: We use `ruff` to check for syntax errors, undefined names, unused variables, and import sorting.
  ```bash
  make lint
  ```
- **Formatting**: `ruff format` auto-fixes formatting.
  ```bash
  make format
  ```
- **Type Checking**: We use `mypy` (the typecheck target copies `__init__.py` into a hyphen-free temp dir because the repo path contains a space).
  ```bash
  make typecheck
  ```
- **Testing**: We use `pytest` for unit and regression testing.
  ```bash
  make test
  ```
- **Pre-commit Check**: Runs lint, typecheck, and tests in order. Ensure this passes before opening a pull request:
  ```bash
  make check
  ```

## Submitting Pull Requests

1. **Create a branch** for your changes:
   ```bash
   git checkout -b feature/my-new-feature
   ```
2. **Commit your changes**: Ensure your commit messages are descriptive and follow standard Git guidelines.
3. **Verify checks**: Run `make check` locally to verify that all linter checks and tests pass successfully.
4. **Push to GitHub** and open a Pull Request against the `main` branch.

## Code Style & Design Rules

- **Safety First**: Any modifications to critical hooks must fail-open (swallow exceptions gracefully) so they do not crash the host Hermes Agent.
- **No Thread Leaks**: Ensure background tasks/timers inspect active session counts and do not reschedule themselves when no sessions remain.
- **Cross-Process Safety**: All reads and writes to shared state (`state.json`, `corrections.log`, etc.) must be protected via the cross-process lock `_lock_file` to support parallel agent execution.
