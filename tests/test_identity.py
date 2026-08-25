"""The variant rule, pinned in both directions (#668).

⛔ THE OBVIOUS IMPLEMENTATION IS WRONG AND THESE TESTS EXIST TO STOP IT COMING BACK.
`agent.split("_")[0]` folds `market_shadow` into `market` and `insights_agent` into `insights`,
merging two different agents into one. Both of those names are real on the reference fleet.
"""

import pytest

from provy.identity import agent_base


@pytest.mark.parametrize("name,expected", [
    ("research_GILD", "research"),
    ("research_MRK", "research"),
    ("research_PANW", "research"),
    ("market_AAPL", "market"),
])
def test_strips_a_ticker_suffix(name, expected):
    assert agent_base(name) == expected


@pytest.mark.parametrize("name", [
    "market_shadow",     # a real second agent, not a variant of market
    "insights_agent",    # ditto
    "knowledge_agent",
    "metrics_agent",
    "risk",
    "orchestrator",
])
def test_leaves_a_real_name_alone(name):
    assert agent_base(name) == name


def test_a_lowercase_suffix_is_not_a_variant():
    assert agent_base("research_gild") == "research_gild"


def test_a_long_suffix_is_not_a_ticker():
    assert agent_base("research_TOOLONG") == "research_TOOLONG"


def test_a_bare_uppercase_name_survives():
    # An earlier version of this rule returned an empty string here.
    assert agent_base("RISK") == "RISK"


@pytest.mark.parametrize("value", [None, ""])
def test_empty_input_passes_through(value):
    assert agent_base(value) == value


def test_only_the_last_segment_is_stripped():
    assert agent_base("deep_research_GILD") == "deep_research"
