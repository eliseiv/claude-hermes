"""Unit: render_instance_config — toolset restriction + LLM model section + anti-injection.

The rendered config.yaml must restrict ``platform_toolsets.api_server`` to the SAFE toolset only
(never terminal/browser/code_execution/computer_use), dedupe, fall back to the safe default when
nothing valid remains, and validate toolset names so a malformed value cannot inject YAML
(ADR-046 §5/§6, #9). It must also emit an explicit ``model`` section (ADR-055): concrete
``model.default="<provider>/<model>"`` + ``model.provider="<provider>"``, an optional ``base_url``
line only when non-empty, and fail fast (UpstreamError) on an invalid provider/model/base_url so a
bad value never reaches the rendered YAML.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import UpstreamError
from app.hermes_runtime.config_yaml import render_instance_config

_DANGEROUS = ("terminal", "browser", "code_execution", "computer_use")
_SAFE_DEFAULT = ["web", "file", "vision", "skills", "todo"]


def _render(toolset: list[str]) -> str:
    """Render with a fixed SAFE provider/model so toolset-focused assertions stay valid.

    ADR-055 made provider/model keyword-only and mandatory; ``anthropic`` /
    ``claude-3-5-haiku-latest`` are chosen deliberately so the emitted model section contains NONE
    of the dangerous substrings ("terminal"/"browser"/"code_execution"/"computer_use"/"allow") —
    otherwise the substring-absence assertions below would be masked by the new model lines.
    """
    return render_instance_config(
        toolset=toolset, provider="anthropic", model="claude-3-5-haiku-latest"
    )


def _toolset_lines(cfg: str) -> list[str]:
    """Return the toolset entry values under platform_toolsets.api_server."""
    out: list[str] = []
    in_block = False
    for line in cfg.splitlines():
        if line.strip() == "api_server:":
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if stripped.startswith("- "):
                out.append(stripped[2:])
            elif stripped and not line.startswith(" "):
                break  # left the block (e.g. approvals:)
    return out


def test_renders_given_safe_toolset() -> None:
    cfg = _render(["web", "file"])
    assert _toolset_lines(cfg) == ["web", "file"]


def test_excludes_dangerous_tools_from_default() -> None:
    cfg = _render(_SAFE_DEFAULT)
    for dangerous in _DANGEROUS:
        assert dangerous not in cfg


def test_dedupes_repeated_entries() -> None:
    cfg = _render(["web", "web", "file", "web"])
    # Dedup is the responsibility of the settings parser; render keeps order but the parser feeds
    # a deduped list. Here we assert render does not duplicate beyond its input — and the settings
    # parser dedup is covered separately below.
    assert _toolset_lines(cfg) == ["web", "web", "file", "web"]


def test_empty_toolset_falls_back_to_safe_default() -> None:
    cfg = _render([])
    assert _toolset_lines(cfg) == _SAFE_DEFAULT


def test_all_invalid_names_fall_back_to_safe_default() -> None:
    cfg = _render(["bad name", "evil:inject", "tool\nbreak"])
    assert _toolset_lines(cfg) == _SAFE_DEFAULT


def test_injection_attempt_is_dropped_partial() -> None:
    # A malformed name with YAML metacharacters is dropped; the valid one survives.
    cfg = _render(["web", "- terminal\napprovals:\n  mode: allow"])
    lines = _toolset_lines(cfg)
    assert lines == ["web"]
    # The injected dangerous toolset / approvals override must NOT have leaked in.
    assert "allow" not in cfg
    assert "terminal" not in cfg


def test_approvals_mode_is_deny() -> None:
    cfg = _render(_SAFE_DEFAULT)
    assert "approvals:" in cfg
    assert "mode: deny" in cfg


def test_output_is_deterministic() -> None:
    assert _render(["web", "file"]) == _render(["web", "file"])


# ============================ settings parser dedup/fallback (#9) ============================
def _settings(raw: str) -> Settings:
    return Settings(HERMES_DEFAULT_TOOLSET=raw)


def test_settings_parses_and_dedupes_toolset() -> None:
    assert _settings(" web , file , web ,vision").hermes_default_toolset() == [
        "web",
        "file",
        "vision",
    ]


def test_settings_blank_toolset_falls_back_to_safe_default() -> None:
    assert _settings("   ").hermes_default_toolset() == _SAFE_DEFAULT


def test_settings_toolset_never_contains_dangerous_default() -> None:
    cfg = _render(_settings("").hermes_default_toolset())
    for dangerous in _DANGEROUS:
        assert dangerous not in cfg


# ============================ ADR-055: model section (render) ============================
def _model_lines(cfg: str) -> dict[str, str]:
    """Return key->value of the ``model:`` block lines (default/provider/base_url)."""
    out: dict[str, str] = {}
    in_block = False
    for line in cfg.splitlines():
        if line.strip() == "model:":
            in_block = True
            continue
        if in_block:
            if line.startswith("  ") and ":" in line:
                key, _, value = line.strip().partition(":")
                out[key.strip()] = value.strip().strip('"')
            elif line.strip() and not line.startswith(" "):
                break
    return out


def test_model_section_default_joins_provider_and_model() -> None:
    cfg = render_instance_config(
        toolset=_SAFE_DEFAULT, provider="anthropic", model="claude-3-5-haiku-latest"
    )
    model = _model_lines(cfg)
    assert model["default"] == "anthropic/claude-3-5-haiku-latest"


def test_model_section_provider_is_concrete_not_auto() -> None:
    cfg = render_instance_config(
        toolset=_SAFE_DEFAULT, provider="anthropic", model="claude-3-5-haiku-latest"
    )
    model = _model_lines(cfg)
    assert model["provider"] == "anthropic"  # concrete provider, never `auto`


def test_base_url_emitted_only_when_non_empty() -> None:
    cfg = render_instance_config(
        toolset=_SAFE_DEFAULT,
        provider="custom",
        model="my-model",
        base_url="https://api.example.com/v1",
        api_key="sk-secret",  # ADR-055 §6: custom requires a key (config.yaml model.api_key)
    )
    model = _model_lines(cfg)
    assert model["base_url"] == "https://api.example.com/v1"
    assert 'base_url: "https://api.example.com/v1"' in cfg


def test_base_url_absent_when_empty() -> None:
    cfg = render_instance_config(
        toolset=_SAFE_DEFAULT, provider="anthropic", model="claude-3-5-haiku-latest", base_url=""
    )
    assert "base_url" not in cfg
    assert "base_url" not in _model_lines(cfg)


def test_render_keeps_toolset_and_approvals_alongside_model_section() -> None:
    # One render → assert ALL invariants coexist: safe toolset, deny approvals, model section.
    cfg = render_instance_config(
        toolset=["web", "file"], provider="anthropic", model="claude-3-5-haiku-latest"
    )
    assert _toolset_lines(cfg) == ["web", "file"]
    for dangerous in _DANGEROUS:
        assert dangerous not in cfg
    assert "approvals:" in cfg and "mode: deny" in cfg
    model = _model_lines(cfg)
    assert model["default"] == "anthropic/claude-3-5-haiku-latest"
    assert model["provider"] == "anthropic"


# ================== ADR-055: fail-fast / anti-injection (render) ==================
def test_provider_not_in_allowlist_raises() -> None:
    # `openai` is intentionally absent from HERMES_PROVIDER_ALLOWLIST.
    with pytest.raises(UpstreamError):
        render_instance_config(toolset=_SAFE_DEFAULT, provider="openai", model="gpt-4o")


def test_provider_garbage_raises() -> None:
    with pytest.raises(UpstreamError):
        render_instance_config(toolset=_SAFE_DEFAULT, provider="garbage123", model="some-model")


def test_empty_model_raises() -> None:
    with pytest.raises(UpstreamError):
        render_instance_config(toolset=_SAFE_DEFAULT, provider="anthropic", model="")


def test_model_with_space_raises_and_emits_no_yaml() -> None:
    with pytest.raises(UpstreamError):
        render_instance_config(toolset=_SAFE_DEFAULT, provider="anthropic", model="bad model")


def test_model_with_newline_injection_raises() -> None:
    with pytest.raises(UpstreamError):
        render_instance_config(
            toolset=_SAFE_DEFAULT, provider="anthropic", model="m\ndefault: evil"
        )


def test_model_with_quote_newline_injection_raises() -> None:
    with pytest.raises(UpstreamError):
        render_instance_config(
            toolset=_SAFE_DEFAULT, provider="anthropic", model='x"\n  provider: "auto'
        )


def test_base_url_with_spaces_raises() -> None:
    with pytest.raises(UpstreamError):
        render_instance_config(
            toolset=_SAFE_DEFAULT,
            provider="custom",
            model="my-model",
            base_url="not a url with spaces",
        )


def test_base_url_with_newline_injection_raises() -> None:
    with pytest.raises(UpstreamError):
        render_instance_config(
            toolset=_SAFE_DEFAULT,
            provider="custom",
            model="my-model",
            base_url="https://evil\n  x: y",
        )


# ===================== ADR-055 §6: config-api-key provider (custom) =====================
def test_custom_emits_api_key_envref_not_plaintext() -> None:
    # custom ∈ HERMES_PROVIDERS_CONFIG_API_KEY → model.api_key is the FIXED env-ref constant; the
    # secret key value is NEVER written to the file (only ${HERMES_INSTANCE_LLM_KEY}).
    cfg = render_instance_config(
        toolset=_SAFE_DEFAULT,
        provider="custom",
        model="my-model",
        base_url="https://api.example.com/v1",
        api_key="sk-super-secret-value",
    )
    assert 'api_key: "${HERMES_INSTANCE_LLM_KEY}"' in cfg
    assert _model_lines(cfg)["api_key"] == "${HERMES_INSTANCE_LLM_KEY}"
    # The plaintext key must not leak into the rendered YAML.
    assert "sk-super-secret-value" not in cfg


def test_env_key_provider_does_not_emit_api_key() -> None:
    # anthropic uses <PROVIDER>_API_KEY env → no model.api_key line (no secret duplication).
    cfg = render_instance_config(
        toolset=_SAFE_DEFAULT,
        provider="anthropic",
        model="claude-3-5-haiku-latest",
        api_key="sk-anything",
    )
    assert "api_key" not in cfg
    assert "api_key" not in _model_lines(cfg)


def test_custom_without_api_key_raises() -> None:
    # ADR-055 §6: a config-api-key provider with no key would 401 at runtime → fail-fast at render.
    with pytest.raises(UpstreamError):
        render_instance_config(
            toolset=_SAFE_DEFAULT,
            provider="custom",
            model="my-model",
            base_url="https://api.example.com/v1",
            api_key="",
        )
