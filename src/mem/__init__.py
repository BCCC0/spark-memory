"""mem — a stateless, server-owned project memory service.

MVP-0 (the walking skeleton) per ``MEM.md``, ``MEM-CLI.md``, ``MEM-SCHEMA.md``
and ``MEM-SERVER.md``, with the pre-build contradictions settled in
``MEM-RESOLUTIONS.md``.

Nothing in this top-level package imports PyYAML or Starlette. Everyday CLI
commands run on bare Python 3.11+; the local Git summary preflight imports
PyYAML only when invoked.
"""

from .version import __version__

__all__ = ["__version__"]
