# AGENTS.md

Guidance for AI agents and humans working in this repository. Read the README for the project
overview and architecture diagram.

## Hard rules

- **Never modify anything under `upstream/`.** It simulates another team's orders system and is
  read-only. Handle upstream data problems in `pipeline/`, not by changing the generator. Tests
  may import or copy `upstream/generate.py`, but must not change it.
- **`FAULT` affects only upstream.** Only `upstream/generate.py` reads it. Never make
  `pipeline/run.py` or `tools/totals.py` read `FAULT` or branch on it; the pipeline must react to
  the data it receives.
- **Keep the order-file contract** (README, "Order-file contract") and the `orders` table schema
  unchanged unless the task explicitly asks for a change.
- **Log line formats are part of the interface.** People and tests read the `loaded ...`,
  `wrote ...`, and totals lines. Keep them byte-for-byte unless asked to change them.
- Runtime code is standard library only. Add a runtime dependency only if the task requires one.
- Do not commit anything under `data/`, `.env`, or `.local/`.

## Setup

```sh
python3.12 -m venv .venv && . .venv/bin/activate
python -m pip install --upgrade pip && python -m pip install --group dev
```

Python 3.12 only (`.python-version`, `requires-python`). There is no build step. No environment
variables are required; `FAULT` is optional (see `.env.example`).

## Commands

| Task | Command |
| --- | --- |
| Run upstream (a batch every 3s) | `python upstream/generate.py` |
| One upstream batch | `python upstream/generate.py --once` |
| Upstream with a fault | `FAULT=schema_drift python upstream/generate.py --once` (`none`, `schema_drift`, `null_spike`) |
| Run the pipeline (polls forever) | `python pipeline/run.py` |
| Run totals (prints every 2s) | `python tools/totals.py` |
| Lint | `ruff check .` |
| Format | `ruff format .` (CI runs `ruff format --check .`) |
| Type check | `mypy` |
| Full test suite with coverage gate | `python -m pytest --cov` |
| One test file | `python -m pytest tests/test_loader.py` |
| Unit tests only | `python -m pytest -m "not integration"` |
| All pre-commit hooks | `pre-commit run --all-files` |

The three scripts run until interrupted. When you drive them from a non-interactive shell, run
them in the background and stop them with SIGTERM (`kill <pid>`); background jobs in a
non-interactive shell ignore Ctrl-C/SIGINT.

## Manual QA path

From a clean state (`data/` absent or deleted, all scripts stopped):

1. `python upstream/generate.py --once` prints `wrote data/landing/orders_<utc>_0001.json orders=N fault=none`.
2. Start `python pipeline/run.py` in the background. Within about 2s it prints
   `loaded orders_<utc>_0001.json orders=N`, and the file moves to `data/processed/`.
3. Run `python tools/totals.py` briefly. It prints `<timestamp> orders=N revenue=<sum> waiting=0`.
4. Fault check: `FAULT=schema_drift python upstream/generate.py --once`. The running pipeline
   exits with `KeyError: 'order_total'`, and the file stays in `data/landing/`.
5. Stop everything and delete `data/` (all of it is generated).

`tests/integration/test_end_to_end.py` automates steps 1-4 against a temporary copy of the repo.

## Before you finish

Run all of these and make sure they pass:

```sh
ruff check . && ruff format --check . && mypy && python -m pytest --cov
```

If you change behavior, commands, or conventions, update README.md and this file in the same
change.

## Code conventions

- Naming (enforced by ruff `N` rules): `snake_case` for functions, variables, and modules;
  `UPPER_SNAKE_CASE` for module constants (`LANDING`, `POLL_INTERVAL`); `PascalCase` for
  classes; exception classes end in `Error`.
- Type hints are required everywhere (mypy `strict = true`).
- Keep functions small and testable. Pass paths and connections in as arguments rather than
  reading module globals inside helpers. `main()` is the only place that wires the real `data/`
  paths. McCabe complexity is capped at 10.
- Log lines use `%`-formatting with `print(..., flush=True)`, matching the existing code.
- TODO comments must link an issue, e.g. `# TODO: handle retries (https://github.com/sudar-path/auto-sdlc/issues/12)`
  (ruff `TD` rules).
- Line length is 120; ruff formats the code.

## Testing conventions

- pytest only. Files are named `tests/test_*.py` and test functions `test_*`
  (configured in `pyproject.toml`).
- Unit tests use the fixtures in `tests/conftest.py`: `conn` is an in-memory SQLite connection
  with the schema applied, and `landing`/`processed` are `tmp_path` directories. Build order
  files with `tests/factories.py`.
- Tests must never touch the real `data/` directory. To test `main()`, monkeypatch the module path
  constants and `time.sleep`.
- End-to-end tests live in `tests/integration/`, carry the `integration` marker, and run the real
  scripts as subprocesses inside a `tmp_path` copy of the repo.
- When you add behavior, add a test for it. CI fails if coverage of `pipeline/` and `tools/` drops below 90%.

## Repository layout

```
upstream/generate.py        simulated orders system (read-only)
pipeline/run.py             loader: connect(), load_file(), process_landing(), main()
tools/totals.py             read-only totals report: read_totals(), main()
tests/                      unit tests and fixtures; tests/integration/ for end-to-end tests
pyproject.toml              project metadata, pinned dev deps, ruff/mypy/pytest/coverage config
.pre-commit-config.yaml     git hooks; keep tool versions in sync with pyproject.toml
.github/                    CI workflow, PR and issue templates, CODEOWNERS
```
