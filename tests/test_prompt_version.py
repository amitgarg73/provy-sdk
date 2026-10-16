"""What changed before it broke: the prompt version and the model on every agent step (argus#1735).

⛔ WHY THIS EXISTS. On production 0 of 10,614 spans carried a `prompt_version` and 4% a `model`. Nothing asked for them and nothing said they were missing, so
"what changed before it broke" could not be answered for anyone, and the history cannot be backfilled. These tests pin the three behaviours that fix that:
a prompt is versioned by hashing it once, a gap is said out loud at the end of a run, and nothing is ever refused or altered because of it.
"""
import provy.client as client_mod
from provy import prompt_hash
from provy.client import ProvyClient
from provy.versioning import VersionCoverage


class _Ok:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(client_mod, "post_with_retry", lambda url, payload, headers, **kw: sent.append(payload) or _Ok())
    monkeypatch.setenv("PROVY_EMIT", "1")
    monkeypatch.delenv("PROVY_PROMPT_VERSION", raising=False)
    return sent


def _client():
    return ProvyClient(ingest_key="k", base_url="http://example.invalid", buffered=False)


def test_hash_is_stable_and_ignores_whitespace():
    a = prompt_hash("You are careful.\n  Answer only from policy.")
    assert a == prompt_hash("You are careful. Answer only from policy.")
    assert a.startswith("p-") and len(a) == 12
    assert a != prompt_hash("You are careful. Answer from anything.")


def test_set_prompt_versions_every_agent_step_of_that_agent(monkeypatch):
    sent = _capture(monkeypatch)
    c = _client()
    v = c.set_prompt("research", "Find the facts.")
    c.trace(session_id="s", agent="research", step_type="agent_message", outcome="ok")
    c.trace(session_id="s", agent="research_GILD", step_type="decision", outcome="ok")   # fan-out finds its base agent
    assert [p["prompt_version"] for p in sent] == [v, v]


def test_an_explicit_version_on_the_call_wins(monkeypatch):
    sent = _capture(monkeypatch)
    c = _client()
    c.set_prompt("research", "Find the facts.")
    c.trace(session_id="s", agent="research", step_type="agent_message", outcome="ok", prompt_version="mine-7")
    assert sent[0]["prompt_version"] == "mine-7"


def test_a_tool_call_is_never_given_a_prompt_version(monkeypatch):
    sent = _capture(monkeypatch)
    c = _client()
    c.set_prompt("research", "Find the facts.")
    c.trace(session_id="s", agent="research", step_type="tool_call", outcome="ok", tool_name="search")
    assert "prompt_version" not in sent[0]


def test_the_environment_default_applies_to_an_agent_that_registered_nothing(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setenv("PROVY_PROMPT_VERSION", "fleet-3")
    _client().trace(session_id="s", agent="risk", step_type="decision", outcome="ok")
    assert sent[0]["prompt_version"] == "fleet-3"


def test_nothing_is_sent_that_was_not_asked_for(monkeypatch):
    sent = _capture(monkeypatch)
    _client().trace(session_id="s", agent="risk", step_type="decision", outcome="ok")
    assert "prompt_version" not in sent[0] and "model" not in sent[0]   # silence is not a statement


def test_coverage_counts_agent_steps_and_only_model_steps_for_model():
    cov = VersionCoverage()
    cov.note("agent_message", False, False, True)    # a model step with neither
    cov.note("agent_message", True, True, True)      # a model step with both
    cov.note("decision", False, False, False)        # plain code: no model expected
    cov.note("tool_call", False, False, False)       # not an agent step at all
    assert cov.report() == {"agent_steps": 3, "no_prompt_version": 2, "model_steps": 2, "no_model": 1}


def test_the_gap_is_said_once_and_quiet_silences_it(capsys, monkeypatch):
    monkeypatch.delenv("PROVY_QUIET", raising=False)
    cov = VersionCoverage()
    cov.note("agent_message", False, False, True)
    cov.say()
    cov.say()
    err = capsys.readouterr().err
    assert err.count("carried no prompt_version") == 1 and "carried no model name" in err
    quiet = VersionCoverage()
    quiet.note("agent_message", False, False, True)
    monkeypatch.setenv("PROVY_QUIET", "1")
    quiet.say()
    assert capsys.readouterr().err == ""


def test_a_fully_labelled_run_says_nothing(capsys, monkeypatch):
    monkeypatch.delenv("PROVY_QUIET", raising=False)
    cov = VersionCoverage()
    cov.note("agent_message", True, True, True)
    cov.say()
    assert capsys.readouterr().err == ""


def test_the_client_reports_coverage(monkeypatch):
    _capture(monkeypatch)
    c = _client()
    c.trace(session_id="s", agent="research", step_type="agent_message", outcome="ok", tokens_in=10, tokens_out=5)
    assert c.coverage_report() == {"agent_steps": 1, "no_prompt_version": 1, "model_steps": 1, "no_model": 1}


def test_the_otel_exporter_counts_from_the_attribute_names_the_server_reads():
    exp = client_mod.ProvyExporter.__new__(client_mod.ProvyExporter)   # counting needs no network and no OpenTelemetry install
    exp._coverage = VersionCoverage()
    exp._note_versions({"argus.step_type": "agent_message", "argus.prompt_version": "p-1", "argus.model": "m", "gen_ai.usage.input_tokens": 9})
    exp._note_versions({"provy.step_type": "decision", "gen_ai.usage.input_tokens": 4})
    exp._note_versions({"argus.step_type": "tool_call"})
    assert exp.coverage_report() == {"agent_steps": 2, "no_prompt_version": 1, "model_steps": 2, "no_model": 1}
