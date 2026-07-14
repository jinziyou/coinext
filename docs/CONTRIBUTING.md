# Contributing to Coinext

## Layout

Keep the eight root lifecycle modules. Inside each module:

```text
<lifecycle>/
  crates/      # Rust crates (coinext-*)
  python/      # Pure Python packages (coinext_*)
  services/    # Deployable entrypoints (optional)
  config/      # foundation only — default YAML
  notebooks/   # strategy-research only
  examples/    # optional
  README.md    # short module index
```

Do **not** reintroduce `component/rust|python` nesting or `*/service/*` double wrappers.

## Adding a Rust crate

1. Create `<lifecycle>/crates/coinext-foo/`.
2. Add to root `Cargo.toml` `[workspace.members]` (or `exclude` if heavy live-edge).
3. Prefer `.workspace = true` for deps; excluded crates use explicit path deps under `*/crates/`.
4. Live-edge builds: `CARGO_TARGET_DIR=target/live-edge` via `just test-live-edge`.

## Adding a Python package

1. Create `<lifecycle>/python/coinext_foo/` with `__init__.py` and a minimal `pyproject.toml`
   (copy from a sibling package; setuptools `package-dir` maps `coinext_foo` → `.`).
2. Add the path to root `pyproject.toml` `[tool.uv.workspace].members` and `[tool.uv.sources]`,
   and list `coinext_foo` under root `[project].dependencies`.
3. Keep import name `coinext_foo` stable. `uv sync` installs workspace members; pytest also keeps
   lifecycle `pythonpath` as a zero-install fallback.

## Local checks

```bash
just test              # Rust workspace
just test-live-edge    # excluded crates → target/live-edge
just py-setup && just py-build && just py-test
just py-lint
just compose-check
just clean-targets     # reclaim nested/workspace cargo targets
```

## Python Docker images

- Install workspace packages with `uv sync --frozen --no-dev --extra …` into `/opt/venv`.
- Install `coinext_py` wheel from the maturin stage when needed (api/trader).
- Set `COINEXT_SERVICE_PYTHONPATH` to the thin service app dir only; use
  `operations-interface/deployment/docker/entrypoint-python.sh`.
- Do **not** reintroduce multi-root `PYTHONPATH` for installed packages.

## Docs

- Design truth: root `ARCHITECTURE.md` (English).
- Operator quick start: root `README.md` (Chinese).
- Status labels: `docs/STATUS.md` (`verified` / `partial` / `scaffold` / `deferred`).
- Package `__init__.py` top docs: one-line role + status — avoid re-stating the parity essay.
