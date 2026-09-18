"""`app.lab` — material that exists only for the eval laboratory.

Inert data, never behaviour: the strings the lab's fixture writers stamp onto
rows. Its own package because its callers sit on both sides of the ADR 0006
import contract, and it is listed in that contract's `source_modules`, so it
can never grow an import of `app.mcp` without CI saying so.
"""

__all__: list[str] = []
