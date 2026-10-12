"""`ProvyExporter(endpoint=...)` must work whether it is given a base URL or the full OTLP URL (argus#1684)."""
import pytest

pytest.importorskip("opentelemetry.sdk.trace")
from provy import ProvyExporter
import provy.client as client_mod

PATH = "/api/otlp/v1/traces"


@pytest.mark.parametrize("given,expected", [
    ("https://dev.provy.ai", "https://dev.provy.ai" + PATH),
    ("https://dev.provy.ai/", "https://dev.provy.ai" + PATH),
    ("https://dev.provy.ai" + PATH, "https://dev.provy.ai" + PATH),
    ("https://dev.provy.ai" + PATH + "/", "https://dev.provy.ai" + PATH),
])
def test_either_spelling_reaches_the_same_url(given, expected):
    assert ProvyExporter(api_key="k", endpoint=given).endpoint == expected


def test_the_path_is_never_doubled():
    assert ProvyExporter(api_key="k", endpoint="http://x.invalid" + PATH).endpoint.count(PATH) == 1


def test_left_out_it_follows_the_configured_base(monkeypatch):
    monkeypatch.setattr(client_mod, "PROVY_BASE_URL", "https://pre.example")
    assert ProvyExporter(api_key="k").endpoint == "https://pre.example" + PATH
