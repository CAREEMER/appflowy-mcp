# AGENTS.md

Guidance for agents (and humans) working on **appflowy-mcp**.

## Project layout

- `src/appflowy_mcp/` — the package.
  - `config.py` — settings + token/scope parsing from env and config files.
  - `appflowy.py` — async AppFlowy REST/collab client.
  - `access.py` — token-scoped access control over workspaces and page subtrees.
  - `blocks.py` — Yjs/CRDT document decoding and block editing helpers.
  - `server.py` — FastMCP tools + entrypoint.
- `tests/` — the test suite (one file per source module).

## Commands

```bash
uv sync            # install runtime + dev dependencies
uv run pytest      # run all tests with the coverage gate
uv run ruff check  # lint
```

## Definition of Done

A change is **done** only when all of the following hold:

1. **100% test coverage.** Line *and* branch coverage of `appflowy_mcp` must be
   100%. This is enforced by `--cov-fail-under=100` in `pyproject.toml`; never
   lower it. New code ships with the tests that cover it. Genuinely unreachable
   lines (optional-import fallbacks, defensive `raise`s after exhaustive loops)
   are excluded with an explicit `# pragma: no cover` and a one-line reason —
   not by relaxing the threshold.

2. **One test = one small piece of behaviour.** Each test exercises a single
   function or branch and asserts one thing. Do not bundle unrelated assertions
   into a "kitchen sink" test. Prefer many small tests over a few large ones.

3. **Clear test names.** A test's name states what it verifies, in the form
   `test_<unit>_<behaviour>` — e.g. `test_scope_parse_empty_raises`,
   `test_request_reauthenticates_once_on_401`. Reading the name alone should
   tell you what broke when it fails.

4. **No superfluous comments.** Tests should read as self-explanatory.
   Comments are only for non-obvious setup (e.g. why a CRDT document is shaped a
   certain way), never to restate what the code plainly does.

5. **Lint passes.** `uv run ruff check` is clean.

6. **The pipeline gates on tests.** The Docker image build `needs: test`
   (`.github/workflows/docker.yml`); a failing test or a coverage drop must
   block the image from being built. Do not work around this gate.

## Conventions

- Tests use `pytest` with `asyncio_mode = "auto"`; write `async def test_...`
  directly for coroutine code, no decorator needed.
- Network and CRDT dependencies are faked, not mocked against the wire. See
  `tests/conftest.py` for the `FakeClient`, `FakeResponse`, and `build_collab`
  helpers.
- Match the surrounding code's style, naming, and comment density.
