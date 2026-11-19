from typing import Any, Optional, Sequence, Tuple

import pytest

import datamodel_code_generator.http


@pytest.fixture
def legacy_httpx_get_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch remote schemas through a plain `httpx.get` call as 0.16.1 did.

    The backported SSRF-safe fetcher resolves DNS and connects through its own
    transport, which bypasses the `httpx.get` mocks used by the existing remote
    $ref tests. Those tests cover $ref resolution and output, not the network
    layer, so they keep the original call shape through this fixture.
    The fetcher itself is covered by tests/test_security_backport.py.
    """

    def get_body(
        url: str,
        headers: Optional[Sequence[Tuple[str, str]]] = None,
        ignore_tls: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        import httpx

        return httpx.get(url, headers=headers, verify=not ignore_tls).text

    monkeypatch.setattr(datamodel_code_generator.http, 'get_body', get_body)
