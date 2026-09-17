"""Configuration, the provider registry, and the pieces main.py wires up.

These are the decisions made once at startup, in a process that then runs for
weeks. A wrong one does not fail a request; it fails every request, or -- the
worse case -- silently does something other than what was configured.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nova.ai.errors import AIConfigurationError
from nova.ai.offline import OfflineEmbeddingProvider
from nova.ai.openai_compatible import OpenAICompatibleEmbeddingProvider
from nova.ai.padded import PaddedEmbeddingProvider
from nova.ai.registry import EMBEDDING_DIMENSIONS, build_embedding_provider
from nova.core.config import AISettings, Settings, get_settings
from nova.main import create_app


class TestBuildingTheEmbeddingProvider:
    def test_the_lexical_one_is_the_default(self) -> None:
        """Good enough to exercise storage and retrieval with no model
        server, no key, no network and no per-run cost."""
        provider = build_embedding_provider(AISettings())

        assert provider.name == "lexical"
        assert provider.dimensions == EMBEDDING_DIMENSIONS

    def test_the_hash_one_is_built_when_asked_for(self) -> None:
        provider = build_embedding_provider(AISettings(embedding_provider="hash"))

        assert isinstance(provider, OfflineEmbeddingProvider)
        assert provider.dimensions == EMBEDDING_DIMENSIONS

    def test_the_openai_compatible_one_is_built_from_configuration(self) -> None:
        """The width is taken from the column rather than from settings.

        A provider built at a different width would write vectors the
        ``memories`` column rejects, one row at a time, at retrieval.
        """
        provider = build_embedding_provider(
            AISettings(
                embedding_provider="openai_compatible",
                openai_base_url="http://192.168.1.20:1234/v1",
                embedding_model="text-embedding-3-small",
                openai_api_key="sk-test",  # type: ignore[arg-type]
            )
        )

        assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
        assert provider.dimensions == EMBEDDING_DIMENSIONS

    def test_it_is_built_without_a_key_for_a_local_server(self) -> None:
        """LM Studio and llama.cpp want no key, and sending an empty bearer
        token makes some of them refuse the request."""
        provider = build_embedding_provider(
            AISettings(
                embedding_provider="openai_compatible",
                openai_base_url="http://192.168.1.20:1234/v1",
                openai_api_key=None,
            )
        )

        assert isinstance(provider, OpenAICompatibleEmbeddingProvider)

    def test_a_narrower_model_is_padded_up_to_the_column(self) -> None:
        """nomic-embed-text is 768 wide; the column is 1536. Zero-padding
        preserves every cosine distance, so the narrower model just works."""
        provider = build_embedding_provider(
            AISettings(embedding_provider="openai_compatible", embedding_dimensions=768)
        )

        assert isinstance(provider, PaddedEmbeddingProvider)
        assert provider.dimensions == EMBEDDING_DIMENSIONS
        assert provider.native_dimensions == 768

    def test_a_model_wider_than_the_column_is_refused_at_startup(self) -> None:
        """Not at the first write. Truncation would destroy the geometry, so
        the only honest answers are a migration or a narrower model."""
        with pytest.raises(AIConfigurationError) as caught:
            build_embedding_provider(
                AISettings(embedding_provider="openai_compatible", embedding_dimensions=4096)
            )

        assert caught.value.code == "ai_embedding_dimension_mismatch"
        assert "migration" in str(caught.value)

    def test_the_width_setting_does_not_touch_the_built_in_embedders(self) -> None:
        """It describes the external model. Lexical vectors already stored
        under the name "lexical" must stay comparable with new ones."""
        provider = build_embedding_provider(AISettings(embedding_dimensions=768))

        assert provider.name == "lexical"
        assert provider.dimensions == EMBEDDING_DIMENSIONS

    def test_the_provider_name_carries_the_model(self) -> None:
        """Two models behind one endpoint produce incomparable vectors, and
        the name on each memory row is what keeps them apart."""
        provider = build_embedding_provider(
            AISettings(embedding_provider="openai_compatible", embedding_model="nomic-embed-text")
        )

        assert provider.name == "openai_compatible:nomic-embed-text"


class TestSettingsAreCached:
    def test_the_process_shares_one_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Modules and FastAPI dependencies must agree about configuration;
        two instances read at different moments would not have to."""
        monkeypatch.setenv("NOVA_DATABASE__PASSWORD", "unit-test-password")
        monkeypatch.setenv("NOVA_JWT__SECRET_KEY", "unit-test-secret-key-at-least-thirty-two-chars")
        get_settings.cache_clear()
        try:
            assert get_settings() is get_settings()
        finally:
            get_settings.cache_clear()


class TestTrustedHosts:
    def test_the_host_check_is_off_while_any_host_is_allowed(self, settings: Settings) -> None:
        """The default, and correct on a home network reached by IP where
        there is no name to allow in the first place."""
        assert settings.security.allowed_hosts == ["*"]

        app = create_app(settings)

        assert "TrustedHostMiddleware" not in [m.cls.__name__ for m in app.user_middleware]

    async def test_a_configured_host_list_refuses_anything_else(self, settings: Settings) -> None:
        """What makes NOVA safe to put behind a tunnel.

        Without it, anything that can reach the port can send a Host header
        of its choosing and have NOVA generate links with it.
        """
        named = settings.model_copy(deep=True)
        named.security.allowed_hosts = ["nova.local"]

        app = create_app(named)

        assert "TrustedHostMiddleware" in [m.cls.__name__ for m in app.user_middleware]

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://nova.local"
        ) as http:
            assert (await http.get("/health")).status_code == 200

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.example.com"
        ) as http:
            assert (await http.get("/health")).status_code == 400
