"""Opt-in live integration tests for the Azure OpenAI and AWS Bedrock providers.

Each makes ONE real completion call through Olympus's actual request path
(backend.complete_text) and is skipped unless the relevant credentials are
present, so CI/dev runs without those accounts are a clean no-op. Unit coverage
(URL/header construction, client wiring, routing) lives in test_providers.py;
this proves an end-to-end call against the real service when configured.

Enable:
  Azure   — OLYMPUS_AZURE_TEST_ENDPOINT (https://<res>.openai.azure.com),
            OLYMPUS_AZURE_TEST_KEY, OLYMPUS_AZURE_TEST_DEPLOYMENT
            (optionally OLYMPUS_AZURE_API_VERSION)
  Bedrock — OLYMPUS_BEDROCK_TEST_MODEL (a Bedrock model id) + the standard AWS
            credential chain (env/shared config/instance role) and the AWS extra
            (`pip install 'anthropic[bedrock]'`)
"""

import os

import pytest

from olympus import backend, config

_MSG = [{"role": "user", "content": "Reply with exactly the word: pong"}]


@pytest.mark.skipif(
    not (os.environ.get("OLYMPUS_AZURE_TEST_ENDPOINT")
         and os.environ.get("OLYMPUS_AZURE_TEST_KEY")
         and os.environ.get("OLYMPUS_AZURE_TEST_DEPLOYMENT")),
    reason="Azure test credentials not set (OLYMPUS_AZURE_TEST_*)")
def test_azure_live_completion(monkeypatch):
    monkeypatch.setenv("OLYMPUS_AZURE_DEPLOYMENT",
                       os.environ["OLYMPUS_AZURE_TEST_DEPLOYMENT"])
    s = config.Settings(
        provider="openai",
        model=os.environ["OLYMPUS_AZURE_TEST_DEPLOYMENT"],
        base_url=os.environ["OLYMPUS_AZURE_TEST_ENDPOINT"],
        api_key=os.environ["OLYMPUS_AZURE_TEST_KEY"])
    out = backend.complete_text(s, "You are terse.", _MSG)
    assert isinstance(out, str) and out.strip(), "no text from Azure"


@pytest.mark.skipif(
    not os.environ.get("OLYMPUS_BEDROCK_TEST_MODEL"),
    reason="Bedrock test model not set (OLYMPUS_BEDROCK_TEST_MODEL)")
def test_bedrock_live_completion():
    pytest.importorskip("anthropic")
    try:
        from anthropic import AnthropicBedrock  # noqa: F401
    except Exception:
        pytest.skip("anthropic[bedrock] (boto3) not installed")
    s = config.Settings(provider="bedrock",
                        model=os.environ["OLYMPUS_BEDROCK_TEST_MODEL"])
    out = backend.complete_text(s, "You are terse.", _MSG)
    assert isinstance(out, str) and out.strip(), "no text from Bedrock"
