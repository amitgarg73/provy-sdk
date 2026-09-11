"""
Tenant-side masking: the tokenizer, and the two properties that make it worth having.

⛔ THE POINT OF THESE TESTS IS NOT "SOMETHING GOT REPLACED". It is that a stable pseudonym keeps the
one signal a flat label destroys: whether two mentions are the same person. A test suite that only
checked "the email is gone" would pass just as happily on the scheme that deletes the evidence.
"""

import re

import pytest

from provy.redact import (
    DEFAULT_RULES,
    PROTECTED_KEYS,
    TOKEN_HEX_CHARS,
    passes_luhn,
    redact_text,
    redacting_masker,
    token_for,
    tokenize_text,
    tokenizing_masker,
)

SECRET = "probe-secret-not-a-real-one"


class TestTokenStability:
    def test_same_value_same_token_every_time(self):
        a = tokenize_text("write to ada@example.com", SECRET)
        b = tokenize_text("also write to ada@example.com", SECRET)
        token = re.search(r"\[EMAIL_[0-9a-f]+\]", a).group(0)
        assert token in b

    def test_different_values_get_different_tokens(self):
        out = tokenize_text("from ada@example.com to grace@example.com", SECRET)
        found = re.findall(r"\[EMAIL_[0-9a-f]+\]", out)
        assert len(found) == 2
        assert found[0] != found[1], "two people must not collapse into one token"

    def test_the_secret_changes_the_token(self):
        """Two tenants masking the same person must not produce the same pseudonym."""
        a = tokenize_text("ada@example.com", SECRET)
        b = tokenize_text("ada@example.com", "a-different-tenant-secret")
        assert a != b

    def test_token_is_long_enough_to_make_a_collision_negligible(self):
        # A collision means two different people read as one, so a mix-up would look correct. 48 bits
        # keeps that at roughly two in a million for ten thousand entities; 32 would be one in a
        # hundred, which is far too often for something a verdict rests on.
        assert TOKEN_HEX_CHARS >= 12

    def test_token_carries_its_type_and_is_obviously_synthetic(self):
        # Deliberately not a plausible fake value: the judge must not score an invention as though
        # the agent wrote it, and a human reading the trace must be able to tell.
        out = tokenize_text("ada@example.com", SECRET)
        assert out.startswith("[EMAIL_") and out.endswith("]")
        assert "@" not in out


class TestNormalisation:
    """Normalisation is a correctness requirement: one person must be one token."""

    def test_email_case_and_padding_do_not_split_one_person_in_two(self):
        a = tokenize_text("Ada@Example.COM", SECRET)
        b = tokenize_text("ada@example.com", SECRET)
        assert a == b

    def test_phone_separators_do_not_split_one_person_in_two(self):
        a = tokenize_text("425-555-0134", SECRET)
        b = tokenize_text("425.555.0134", SECRET)
        assert a == b


class TestWhatSurvives:
    """Over-masking is the designed-against failure. These must pass through untouched."""

    def test_a_16_digit_order_id_survives_because_it_fails_luhn(self):
        assert "1234567890123456" in tokenize_text("order 1234567890123456", SECRET)

    def test_a_luhn_valid_card_is_masked(self):
        out = tokenize_text("card 4242424242424242", SECRET)
        assert "4242424242424242" not in out
        assert "[CARD_" in out

    def test_a_bare_ten_digit_run_survives(self):
        assert "4255550134" in tokenize_text("ref 4255550134", SECRET)

    def test_a_person_name_survives_because_no_rule_can_see_it(self):
        # Stated as a test so the limit is not a surprise later: the built-in rules are patterns, and
        # a name has no pattern. Catching names needs a named-entity pass or a customer rule.
        assert "Ada Lovelace" in tokenize_text("refunded Ada Lovelace", SECRET)

    def test_numbers_and_keys_are_untouched(self):
        masker = tokenizing_masker(SECRET)
        out = masker({"realized_pnl": 42.5, "qty": 3, "ok": True, "note": "ada@example.com"})
        assert out["realized_pnl"] == 42.5
        assert out["qty"] == 3
        assert out["ok"] is True
        assert "[EMAIL_" in out["note"]


class TestProtectedKeys:
    def test_join_keys_are_never_masked(self):
        masker = tokenizing_masker(SECRET)
        body = {
            "session_id": "ada@example.com",   # absurd on purpose: even here it must survive
            "entity_id": "TKT-000123",
            "agent": "refund_agent",
            "tool_name": "lookup_customer",
            "span_id": "abc123",
            "outcome": "mailed ada@example.com",
        }
        out = masker(body)
        for k in ("session_id", "entity_id", "agent", "tool_name", "span_id"):
            assert out[k] == body[k], f"{k} is a join key and must not be masked"
        assert "[EMAIL_" in out["outcome"]

    def test_the_protected_set_is_pinned(self):
        # Adding one silently would quietly stop masking a field. Removing one would break joins.
        assert PROTECTED_KEYS == frozenset({
            "session_id", "span_id", "parent_span_id", "entity_id",
            "agent", "step_type", "tool_name", "session_type",
        })


class TestFlatRedactionIsWorse:
    """The argument for tokenizing, made as an assertion rather than a comment."""

    def test_flat_labels_make_a_mix_up_indistinguishable_from_a_correct_run(self):
        correct = "looked up ada@example.com then refunded ada@example.com"
        mixed_up = "looked up ada@example.com then refunded grace@example.com"
        assert redact_text(correct) == redact_text(mixed_up), (
            "this is the defect being demonstrated, not a bug in the test"
        )

    def test_tokens_keep_the_two_apart(self):
        correct = "looked up ada@example.com then refunded ada@example.com"
        mixed_up = "looked up ada@example.com then refunded grace@example.com"
        assert tokenize_text(correct, SECRET) != tokenize_text(mixed_up, SECRET)

    def test_a_correct_run_reads_as_one_person_under_tokenisation(self):
        out = tokenize_text("looked up ada@example.com then refunded ada@example.com", SECRET)
        found = re.findall(r"\[EMAIL_[0-9a-f]+\]", out)
        assert len(found) == 2 and found[0] == found[1]


class TestSecretIsRequired:
    def test_tokenising_without_a_secret_refuses_rather_than_weakening_quietly(self):
        # A bare hash of an email is reversible by dictionary, so defaulting the secret to empty
        # would hand out a pseudonym that is not one.
        with pytest.raises(ValueError, match="secret"):
            tokenize_text("ada@example.com", "")


class TestRulesMirrorTheServer:
    def test_rule_names_are_pinned_so_a_drift_from_the_server_is_visible(self):
        # The server is the source of truth for this list (see the note in provy/redact.py and
        # CONTRACT.md). This SDK cannot ask the server what to mask without first sending the data,
        # so the list is duplicated and pinned here instead.
        assert [r.name for r in DEFAULT_RULES] == [
            "bearer", "api_key", "jwt", "email", "ssn", "card", "phone",
        ]

    def test_credentials_are_masked_before_anything_else_can_partially_match(self):
        out = tokenize_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", SECRET)
        assert "abcdefghijklmnopqrstuvwxyz123456" not in out


class TestLuhn:
    def test_rejects_wrong_lengths_and_bad_checksums(self):
        assert passes_luhn("4242424242424242")
        assert not passes_luhn("1234567890123456")
        assert not passes_luhn("424242")

    def test_token_for_is_deterministic(self):
        assert token_for("x", "email", SECRET) == token_for("x", "email", SECRET)


class TestNesting:
    def test_masks_inside_lists_and_nested_dicts(self):
        masker = redacting_masker()
        out = masker({"steps": [{"note": "ada@example.com"}, {"note": "fine"}]})
        assert out["steps"][0]["note"] == "[REDACTED_EMAIL]"
        assert out["steps"][1]["note"] == "fine"

    def test_deep_structures_do_not_hang(self):
        body = cur = {}
        for _ in range(80):
            cur["next"] = {}
            cur = cur["next"]
        cur["note"] = "ada@example.com"
        tokenizing_masker(SECRET)(body)  # depth-bounded, must simply return
