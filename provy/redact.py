"""
Tenant-side masking: strip or pseudonymise sensitive values before they ever leave the caller.

⛔ WHY THIS IS THE IMPORTANT HALF OF THE PRIVACY STORY. Provy masks content again on the way out to a
model provider, and that server-side pass cannot be switched off. But by then we are already holding
the data. The customer is the controller, holds the identifiers, and knows which of their own fields
are sensitive. Masking here means the raw value never arrives, and there is nothing to argue about in
a security review. Every comparable tool in the category (Langfuse, LangSmith, Arize, Weave, Sentry)
puts its primary hook in the customer's own process for the same reason.

⛔ THE TWO PASSES COMPOSE, THEY DO NOT REPLACE EACH OTHER. Provy's egress masking runs regardless of
what happens here, so a gap in your masker is less early protection, never an open door. Sentry's
layered model is the one worth copying: the client decides what it is willing to send, and the server
scrubs again by default.

## Flat labels destroy evidence. Stable tokens do not.

This is the part that decides whether masking is safe to turn on, and it is not obvious.

Replace every email with a single `[REDACTED_EMAIL]` and two very different runs become
byte-identical:

    looked up [REDACTED_EMAIL] ... issued refund to [REDACTED_EMAIL]     # correct
    looked up [REDACTED_EMAIL] ... issued refund to [REDACTED_EMAIL]     # refunded the WRONG person

Nothing downstream can tell those apart: not a person reading the trace, not the LLM judge scoring
it, not the reconciliation that decides whether the work item succeeded. Flat masking deletes exactly
the evidence Provy exists to find, and it deletes it silently.

`tokenize()` instead maps each distinct value to a stable pseudonym, so the same person is the same
token everywhere and a different person visibly is not:

    looked up [EMAIL_a91c4e2f7b03] ... issued refund to [EMAIL_a91c4e2f7b03]   # correct
    looked up [EMAIL_a91c4e2f7b03] ... issued refund to [EMAIL_5d7e08b1cc42]   # the defect, still visible

Grouping, dedup and joins keep working for the same reason. This is what Google DLP, Skyflow and
Protecto all sell as the answer to "de-identification breaks my analytics", and it applies at least
as strongly to judging.

## Three decisions worth knowing about

**The token is an HMAC, not a hash.** A plain SHA of `ada@example.com` is a constant anyone can look
up, so a bare hash of a low-entropy value (an email, a ten-digit phone) is reversible by dictionary.
`secret` keys the HMAC. **Hold that secret yourself and Provy cannot reverse the tokens at all**,
which is the strongest position available to you and the reason this argument belongs in your process
and not ours.

**The token is deliberately NOT format-preserving, and not a fake name.** Some tools swap a real name
for a plausible invented one. Do not do that here: the judge would score the invention as if it were
the agent's real output, and a person reading the trace could not tell. An obviously-synthetic token
carrying its type is honest to both readers.

**12 hex characters, because a collision is a wrong answer and not just a clash.** Two different
people sharing a token means a mix-up reads as correct. At 48 bits, ten thousand distinct entities
collide with probability around two in a million; at the 32 bits an 8-character token would give, the
same population collides better than one time in a hundred, which is far too often for something a
verdict rests on.

## Identity is yours to choose, and it is not masked here

`session_id`, `span_id`, `parent_span_id`, `entity_id`, `agent`, `step_type` and `tool_name` are
never touched. They are the join keys: `entity_id` is what ties a trace to the outcome you later
report for the same work item, and `agent` is what the fleet view groups by. Masking them would not
protect anything a reader could not already infer, and it would break reconciliation.

So if a ticket id or an order id is itself sensitive to you, **pass one that is already pseudonymous**
rather than asking the SDK to mask it. You control what you put in that field; we cannot mask it and
still join on it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any, Callable, Iterable, Pattern

log = logging.getLogger("provy.sdk")

# Fields that are identity or structure, never content. See the module docstring.
PROTECTED_KEYS: frozenset[str] = frozenset({
    "session_id", "span_id", "parent_span_id", "entity_id",
    "agent", "step_type", "tool_name", "session_type",
})

# 48 bits. See the module docstring for why this is not 32.
TOKEN_HEX_CHARS = 12


class Rule:
    """One thing to look for, and what to put in its place.

    `verify` exists for the cases where the shape is not enough: a 16-digit run is a card only if it
    passes Luhn, and an order id of the same length must survive.
    """

    __slots__ = ("name", "pattern", "verify", "normalize")

    def __init__(
        self,
        name: str,
        pattern: Pattern[str],
        verify: Callable[[str], bool] | None = None,
        normalize: Callable[[str], str] | None = None,
    ):
        self.name = name
        self.pattern = pattern
        self.verify = verify
        # ⛔ NORMALISATION IS A CORRECTNESS REQUIREMENT, NOT A TIDINESS ONE. `Ada@Example.COM` and
        # `ada@example.com` are one person. Tokenising them differently invents a mix-up that never
        # happened, and the judge would be right to call it a defect.
        self.normalize = normalize


def passes_luhn(digits: str) -> bool:
    """Luhn check, so a card is masked and a same-length identifier is not."""
    only = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(only) <= 19:
        return False
    total, double = 0, False
    for d in reversed(only):
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _digits_only(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


# ⛔ THIS LIST MIRRORS THE SERVER'S AND THE SERVER IS THE SOURCE OF TRUTH. It is duplicated here on
# purpose, because the whole point of this module is to run before anything reaches the server, and a
# client that has to ask the server what to mask has already sent the data. `CONTRACT.md` records the
# obligation; `tests/test_redact.py` pins the names so a drift is visible on this side.
#
# Credentials come first: a half-masked secret is still a leaked secret.
DEFAULT_RULES: list[Rule] = [
    Rule("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    Rule("api_key", re.compile(r"\b(?:sk|pk|rk|provy)[-_][A-Za-z0-9_-]{12,}")),
    Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    Rule(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        normalize=lambda s: s.strip().lower(),
    ),
    Rule("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), normalize=_digits_only),
    Rule(
        "card",
        re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
        verify=passes_luhn,
        normalize=_digits_only,
    ),
    # Separators required. A bare ten-digit run is indistinguishable from an identifier, so it is
    # left alone rather than guessed at.
    Rule(
        "phone",
        re.compile(
            r"(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?\d{3}[ .-]?\d{4}|\b\d{3}[ .-]\d{3}[ .-]\d{4}\b)"
        ),
        normalize=_digits_only,
    ),
]


def token_for(value: str, kind: str, secret: str) -> str:
    """The stable pseudonym for one value. Same value and secret, same token, forever."""
    mac = hmac.new(secret.encode("utf-8"), f"{kind}:{value}".encode("utf-8"), hashlib.sha256)
    return f"[{kind.upper()}_{mac.hexdigest()[:TOKEN_HEX_CHARS]}]"


def _replace(text: str, rules: Iterable[Rule], render: Callable[[str, Rule], str]) -> str:
    out = text
    for rule in rules:
        def sub(m: "re.Match[str]", _rule: Rule = rule) -> str:
            found = m.group(0)
            if _rule.verify and not _rule.verify(found):
                return found
            return render(found, _rule)
        out = rule.pattern.sub(sub, out)
    return out


def redact_text(text: str, rules: Iterable[Rule] | None = None) -> str:
    """Replace every match with a flat type label.

    ⛔ PREFER `tokenize_text`. This loses the ability to tell one entity from another, which is the
    failure described at the top of this file. It is here for the case where you want the value gone
    with no derived value of any kind left behind, and you accept that cost knowingly.
    """
    return _replace(text, rules or DEFAULT_RULES, lambda _f, r: f"[REDACTED_{r.name.upper()}]")


def tokenize_text(text: str, secret: str, rules: Iterable[Rule] | None = None) -> str:
    """Replace every match with a stable pseudonym. This is the one to use."""
    if not secret:
        raise ValueError(
            "tokenize_text needs a secret. Without one the tokens are a plain hash and a plain hash "
            "of an email is reversible by dictionary. Generate one once, keep it, and do not share it "
            "with Provy if you want the tokens to be irreversible to us."
        )

    def render(found: str, rule: Rule) -> str:
        value = rule.normalize(found) if rule.normalize else found
        return token_for(value, rule.name, secret)

    return _replace(text, rules or DEFAULT_RULES, render)


def _walk(value: Any, fn: Callable[[str], str], depth: int = 0) -> Any:
    """Apply `fn` to every string VALUE, leaving keys, numbers and booleans alone.

    ⛔ NUMBERS AND KEYS ARE NEVER TOUCHED. A contract condition like `realized_pnl > 0` is graded
    against a number, and a masked number is a broken check rather than a protected one.

    Protected keys are skipped wholesale: see PROTECTED_KEYS and the module docstring.
    """
    if depth > 32:
        # Depth-bounded so a self-referential structure cannot hang the caller's agent.
        return value
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {
            k: (v if k in PROTECTED_KEYS else _walk(v, fn, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_walk(v, fn, depth + 1) for v in value]
    return value


def tokenizing_masker(secret: str, rules: Iterable[Rule] | None = None) -> Callable[[dict], dict]:
    """The masker to pass as `ProvyClient(mask=...)`. Stable pseudonyms, joins preserved."""
    return lambda body: _walk(body, lambda s: tokenize_text(s, secret, rules))


def redacting_masker(rules: Iterable[Rule] | None = None) -> Callable[[dict], dict]:
    """Flat labels. Read the warning on `redact_text` before choosing this over the tokenizer."""
    return lambda body: _walk(body, lambda s: redact_text(s, rules))
