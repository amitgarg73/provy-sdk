"""Agent identity: one rule, one place.

⛔ THIS RULE ALREADY EXISTED TWICE ON THE SERVER AND ZERO TIMES IN ANY CLIENT, and that asymmetry
was a real defect. Provy's `lib/agentUtils.ts` and `lib/agent-identity.ts` both strip a per-entity
suffix so `research_GILD` groups under `research`. Every client looked its parent span up by exact
name, so a fan-out agent missed, fell back to the session root, and the trace arrived flat.

Measured on the production trading fleet: all 24 `research_*` spans in one session sat beside
`agent:research` instead of under it, while `risk` — which has no variants — nested correctly. The
tree was not broken by the tree builder; it was never sent as a tree.

⛔ THE PATTERN IS 1-5 UPPERCASE LETTERS AND NOTHING LOOSER. `agent.split("_")[0]` is the obvious
version and it is wrong: it folds `market_shadow` into `market` and `insights_agent` into
`insights`, silently merging two different agents into one. Mirrors TICKER_SUFFIX in
`lib/agent-identity.ts` exactly.
"""

from __future__ import annotations

import re

_VARIANT_SUFFIX = re.compile(r"^[A-Z]{1,5}$")


def agent_base(agent: str | None) -> str | None:
    """`research_GILD` -> `research`. `market_shadow` -> `market_shadow`.

    Returns the input unchanged when there is no variant suffix, including for None and for a name
    with no underscore at all.
    """
    if not agent:
        return agent
    parts = agent.split("_")
    if len(parts) < 2:
        return agent
    return "_".join(parts[:-1]) if _VARIANT_SUFFIX.match(parts[-1]) else agent
