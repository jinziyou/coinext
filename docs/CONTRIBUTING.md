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

1. Create `<lifecycle>/python/coinext_foo/` with `__init__.py`.
2. Ensure the parent `*/python` dir is on pytest `pythonpath` in root `pyproject.toml`.
3. Keep import name `coinext_foo` stable.

## Local checks

```bash
just test              # Rust workspace
just test-live-edge    # excluded crates → target/live-edge
just py-setup && just py-build && just py-test
just py-lint
just compose-check
just clean-targets     # reclaim nested/workspace cargo targets
```

## Docs

- Design truth: root `ARCHITECTURE.md` (English).
- Operator quick start: root `README.md` (Chinese).
- Status labels: `docs/STATUS.md` (`verified` / `partial` / `scaffold` / `deferred`).
- Module top comments: one-line role + status + link to ARCHITECTURE — avoid re-stating the parity essay.
