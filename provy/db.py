from __future__ import annotations

import os

try:
    from supabase import create_client, Client
except ImportError as exc:  # supabase ships in the 'engine' extra (legacy direct-DB path)
    raise ImportError(
        "Direct-DB features (the local engine, TraceLogger persistence) need the "
        "'engine' extra. Install it with: pip install \"provy-sdk[engine]\". "
        "For new pipelines, prefer the ingest API via ProvyClient (no DB credentials)."
    ) from exc

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_KEY"],
        )
    return _client


def reset_client() -> None:
    """Force re-initialization on next get_client() call. Useful in tests."""
    global _client
    _client = None
