from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jev import JevSelector


def selector_response(**changes: Any) -> dict[str, Any]:
    return {
        "model": "jev-test",
        "answers": {
            "vendor": {
                "choice": "vendor_0",
                "confidence": 0.96,
                "probabilities": {"vendor_0": 0.98, "none": 0.02},
                **changes,
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_jev_request_and_vendor_mapping() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["state"] == {"descriptor": "SQ * ACME"}
        assert body["questions"]["vendor"]["criteria"] == {
            "vendor_0": "Acme",
            "none": "No clear match to a known vendor",
        }
        return httpx.Response(200, json=selector_response())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        decision = JevSelector(client, api_key="test-key")("SQ * ACME", ["Acme"])
    assert decision.vendor == "Acme"
    assert decision.confidence == 0.96
    assert decision.usage == {"input_tokens": 100, "output_tokens": 20}


@pytest.mark.parametrize(
    "changes",
    [
        {"confidence": 0.2},
        {"choice": "none", "probabilities": {"vendor_0": 0.02, "none": 0.98}},
    ],
)
def test_jev_abstains(changes: dict[str, Any]) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=selector_response(**changes))
        )
    ) as client:
        assert JevSelector(client, api_key="test")("unknown", ["Acme"]).vendor is None


@pytest.mark.parametrize(
    "changes",
    [
        {"choice": "invented"},
        {"confidence": float("nan")},
        {"confidence": 1.1},
        {"probabilities": {"vendor_0": 0.2, "none": 0.1}},
        {"probabilities": {"vendor_0": 0.2, "none": 0.8}},
        {"probabilities": {"vendor_0": 0.98, "unexpected": 0.02}},
    ],
)
def test_invalid_answers_fail_closed(changes: dict[str, Any]) -> None:
    # Build bytes to allow a deliberately nonstandard NaN value in the test response.
    payload = json.dumps(selector_response(**changes)).encode()
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
        ) as client,
        pytest.raises(ValueError),
    ):
        JevSelector(client, api_key="test")("descriptor", ["Acme"])


def test_vendor_limit_and_http_errors_are_not_silently_accepted() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))) as client:
        select = JevSelector(client, api_key="test")
        with pytest.raises(ValueError):
            select("descriptor", [str(i) for i in range(255)])
        with pytest.raises(httpx.HTTPStatusError):
            select("descriptor", ["Acme"])
