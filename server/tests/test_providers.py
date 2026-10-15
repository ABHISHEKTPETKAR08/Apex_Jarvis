import pytest

from providers import (
    AzureOpenAIProvider,
    ConfigError,
    MisconfiguredProvider,
    OfflineProvider,
    ProviderError,
    build_provider,
    classify_azure_endpoint,
    load_azure_config,
)

AZ = {
    "AI_PROVIDER": "azure_openai",
    "AZURE_OPENAI_ENDPOINT": "https://myres.openai.azure.com/",
    "AZURE_OPENAI_API_KEY": "secret-key-123",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "jarvis-gpt4o",
    "AZURE_OPENAI_API_VERSION": "2024-10-21",
}


def test_unset_provider_is_offline_with_warning_when_key_present():
    p, warning = build_provider({"ANTHROPIC_API_KEY": "sk-x"})
    assert isinstance(p, OfflineProvider)
    assert "AI_PROVIDER is not set" in warning


def test_explicit_offline():
    p, warning = build_provider({"AI_PROVIDER": "offline"})
    assert isinstance(p, OfflineProvider) and warning is None


def test_unknown_provider_is_misconfigured_not_offline():
    p, _ = build_provider({"AI_PROVIDER": "gemini"})
    assert isinstance(p, MisconfiguredProvider) and not p.configured


def test_anthropic_without_key_does_not_fall_back(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", "/nonexistent-jarvis-test")
    p, _ = build_provider({"AI_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "<anthropic-api-key>"})
    if p.configured:  # an `ant auth login` profile on this machine counts as a real credential
        pytest.skip("Anthropic credential available from the SDK credential chain")
    assert isinstance(p, MisconfiguredProvider)
    with pytest.raises(ProviderError):
        p.respond("s", "hi")


def test_azure_deployments_endpoint():
    p, _ = build_provider(AZ)
    assert isinstance(p, AzureOpenAIProvider)
    st = p.status()
    assert st["protocol"] == "azure_deployments"
    assert st["endpoint_host"] == "myres.openai.azure.com"
    assert st["deployment"] == "jarvis-gpt4o" and st["api_version"] == "2024-10-21"
    assert "secret-key-123" not in repr(st)


def test_azure_v1_endpoint_uses_openai_client_without_api_version():
    cfg = load_azure_config({**AZ, "AZURE_OPENAI_ENDPOINT": "https://myres.services.ai.azure.com/openai/v1/",
                             "AZURE_OPENAI_API_VERSION": ""})
    assert cfg.protocol == "openai_v1" and cfg.base_url.endswith("/openai/v1/") and cfg.api_version == ""
    p = AzureOpenAIProvider(cfg)
    assert type(p.client).__name__ == "OpenAI"


def test_azure_target_uri_parsed():
    uri = "https://myres.openai.azure.com/openai/deployments/jarvis-gpt4o/chat/completions?api-version=2025-01-01-preview"
    cfg = load_azure_config({**AZ, "AZURE_OPENAI_ENDPOINT": uri, "AZURE_OPENAI_API_VERSION": ""})
    assert (cfg.protocol, cfg.deployment, cfg.api_version) == ("azure_deployments", "jarvis-gpt4o", "2025-01-01-preview")
    assert cfg.base_url == "https://myres.openai.azure.com/"


def test_azure_target_uri_conflict_rejected():
    uri = "https://myres.openai.azure.com/openai/deployments/other/chat/completions?api-version=2024-10-21"
    with pytest.raises(ConfigError, match="does not match"):
        load_azure_config({**AZ, "AZURE_OPENAI_ENDPOINT": uri})


@pytest.mark.parametrize("endpoint,msg", [
    ("https://myres.services.ai.azure.com/api/projects/proj1", "project"),
    ("https://myres.services.ai.azure.com/models", "model-inference"),
    ("http://myres.openai.azure.com/", "https"),
    ("https://example.com/", "not an Azure"),
])
def test_unsupported_endpoints_rejected(endpoint, msg):
    with pytest.raises(ConfigError, match=msg):
        classify_azure_endpoint(endpoint)


@pytest.mark.parametrize("missing", ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT_NAME", "AZURE_OPENAI_API_VERSION"])
def test_azure_missing_values_are_misconfigured(missing):
    p, _ = build_provider({**AZ, missing: ""})
    assert isinstance(p, MisconfiguredProvider)
    assert missing in p.status()["error"]
    assert p.check()["ok"] is False


def test_placeholders_count_as_missing():
    p, _ = build_provider({**AZ, "AZURE_OPENAI_API_KEY": "<azure-openai-key>"})
    assert isinstance(p, MisconfiguredProvider)


def test_bad_api_version_format():
    with pytest.raises(ConfigError, match="api-version|API_VERSION"):
        load_azure_config({**AZ, "AZURE_OPENAI_API_VERSION": "v1"})


def test_azure_check_reports_failure_without_secret():
    # Unroutable endpoint: the check must fail cleanly and never echo the key.
    p, _ = build_provider({**AZ, "AZURE_OPENAI_ENDPOINT": "https://jarvis-test-does-not-exist.openai.azure.com/"})
    p.client = p.client.with_options(max_retries=0, timeout=5)
    r = p.check()
    assert r["ok"] is False
    assert "secret-key-123" not in repr(r)
