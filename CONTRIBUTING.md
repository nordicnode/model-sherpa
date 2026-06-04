# Contributing to Model Sherpa

First off, thank you for considering contributing to Model Sherpa! It is people like you who make open source such a great community.

## Local Development Setup

To set up a local development environment:

1. **Fork and Clone**: Fork the repository on GitHub and clone it locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/model-sherpa.git
   cd model-sherpa
   ```

2. **Dependencies**: Create a virtual environment and install the development dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install pyflakes pytest pytest-asyncio typeguard anyio
   ```

## Development Workflow

We use a simple `Makefile` to enforce code quality and correctness:

* **Linting**: We use `pyflakes` to check for syntax errors, undefined names, and unused variables.
  ```bash
  make lint
  ```
* **Testing**: We use `pytest` for unit and regression testing.
  ```bash
  make test
  ```
* **Pre-commit Check**: Runs both the linter and tests in order. Ensure this passes before opening a pull request:
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

* **Safety First**: Any modifications to critical hooks must fail-open (swallow exceptions gracefully) so they do not crash the host Hermes Agent.
* **No Thread Leaks**: Ensure background tasks/timers inspect active session counts and do not reschedule themselves when no sessions remain.
* **Cross-Process Safety**: All reads and writes to shared state (`state.json`, `corrections.log`, etc.) must be protected via the cross-process lock `_lock_file` to support parallel agent execution.
