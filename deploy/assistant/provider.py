"""Optional AI provider for Ask Vault.

Ask Vault works without any AI provider: curated answers from the knowledge
base are returned directly. An operator can opt in to having answers phrased by
Claude. The model is given the knowledge base and told to use nothing else.

Configuration (environment variables, read server-side only):

  ASK_VAULT_PROVIDER   "anthropic" to enable; anything else (or unset) = curated answers only
  ASK_VAULT_API_KEY    API key for the provider (required when enabled)
  ASK_VAULT_MODEL      model id (default: claude-opus-5)
  ASK_VAULT_BASE_URL   API base URL (default: https://api.anthropic.com)

Dedicated variables are used on purpose: the assistant never picks up ambient
credentials or routing (e.g. ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL) from the
environment it happens to run in. Requires `pip install anthropic`; if the
package is missing, the server stays in curated mode and says why.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
# Models that support server-side refusal fallbacks and effort control.
FALLBACK_CAPABLE = {"claude-opus-5", "claude-fable-5-1"}
EFFORT_CAPABLE_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5", "claude-opus-4-8",
                           "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")


class ProviderError(Exception):
    """The provider could not produce an answer; the caller falls back to curated answers."""


class ProviderRefused(ProviderError):
    pass


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 45.0):
        import anthropic  # optional dependency, imported only when enabled

        self._anthropic = anthropic
        self.model = model
        # Explicit key and base URL: never inherit ambient ANTHROPIC_* settings.
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)

    def complete(self, system: str, messages: list[dict]) -> str:
        a = self._anthropic
        params = dict(
            model=self.model,
            max_tokens=8000,
            # The system prompt (rules + whole knowledge base) is identical on every request: cache it.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        if self.model.startswith(EFFORT_CAPABLE_PREFIXES):
            params["output_config"] = {"effort": "low"}  # short explanatory answers
        try:
            if self.model in FALLBACK_CAPABLE:
                # If the model declines, the API retries on a fallback model within the same call.
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-06-01"],
                    fallbacks=[{"model": "claude-opus-4-8"}],
                    **params,
                )
            else:
                resp = self.client.messages.create(**params)
        except a.RateLimitError as e:
            raise ProviderError("rate limited") from e
        except a.APIStatusError as e:
            raise ProviderError(f"HTTP {e.status_code}") from e
        except a.APIConnectionError as e:
            raise ProviderError("connection failed") from e
        if resp.stop_reason == "refusal":
            raise ProviderRefused("model declined")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise ProviderError(f"no text (stop_reason={resp.stop_reason})")
        return text


def from_env(env=None):
    """Return (provider or None, public status dict, secrets to redact). Never exposes the key."""
    env = os.environ if env is None else env
    wanted = (env.get("ASK_VAULT_PROVIDER") or "").strip().lower()
    if wanted != "anthropic":
        return None, {"mode": "curated", "reason": "no AI provider configured"}, ()
    key = env.get("ASK_VAULT_API_KEY") or ""
    if not key:
        return None, {"mode": "curated", "reason": "ASK_VAULT_API_KEY is not set"}, ()
    model = (env.get("ASK_VAULT_MODEL") or DEFAULT_MODEL).strip()
    base_url = (env.get("ASK_VAULT_BASE_URL") or DEFAULT_BASE_URL).strip()
    try:
        provider = AnthropicProvider(key, model, base_url)
    except ImportError:
        return None, {"mode": "curated", "reason": "the anthropic package is not installed"}, (key,)
    return provider, {"mode": "ai", "provider": "anthropic", "model": model}, (key,)
