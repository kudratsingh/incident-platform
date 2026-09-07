"""`app.lab` — material that exists only for the eval laboratory.

Inert data, never behaviour. Nothing in this package runs, decides, or
reaches a database; it holds the strings the lab's fixture writers stamp
onto rows so that every writer stamps the *same* strings and a reader can
check them.

Why it is a package of its own rather than a module beside one of its
callers:

  * Its callers sit on both sides of the ADR 0006 import contract. The
    chaos hooks live in `app.mcp.tools.chaos`, and "nothing outside
    `app.mcp` imports `app.mcp`" is a contract in `pyproject.toml` — so
    `scripts/seed_eval_fixtures.py`, which the API also runs at boot,
    must not reach into the MCP surface to find a shared constant.
    Anything both may import has to sit below both.
  * `app.models` is persistence and `app.utils` is runtime behaviour.
    These strings are neither. Naming the package for what it is keeps a
    reader from mistaking lab furniture for a production code path.

`app.lab` is listed in the import contract's `source_modules`, so it can
never grow an import of `app.mcp` without CI saying so.
"""

__all__: list[str] = []
