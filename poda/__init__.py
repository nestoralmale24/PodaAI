"""Poda AI (formerly deadcode): removes dead code from legacy applications before documenting or migrating them.

Hybrid pipeline: static analysis proposes candidates with evidence, an LLM
(through LiteLLM) validates the ambiguous ones, and a deterministic rewriter
applies the removals to a copy of the project.
"""

__version__ = "2.0.0"

import os as _os

# LiteLLM downloads its model price table from GitHub when it is imported. Use the
# copy bundled with the pinned LiteLLM instead: offline, reproducible costs and no
# outbound request for corporate proxies to block. Set it to "False" to refresh it.
_os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# Formerly "deadcode": variables named DEADCODE_* keep working as PODA_*.
for _name, _value in list(_os.environ.items()):
    if _name.startswith("DEADCODE_"):
        _os.environ.setdefault("PODA_" + _name[len("DEADCODE_"):], _value)
