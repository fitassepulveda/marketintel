"""Provider-agnostic LLM client.

Set `llm.provider` in config/settings.yaml to "gemini" (free tier available)
or "anthropic". Both expose one method:

    client.complete(model, system, prompt, max_tokens) -> response text
"""
import logging
import os
import time

import requests

log = logging.getLogger("llm_client")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Free tier allows 10 requests/minute on gemini-2.5-flash; pace ourselves to stay under.
GEMINI_MIN_SECONDS_BETWEEN_CALLS = 7


class LLMClient:
    def __init__(self, provider: str):
        self.provider = provider
        self._last_call = 0.0
        if provider == "anthropic":
            import anthropic  # imported lazily so it's optional when using gemini
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not key:
                raise SystemExit("Missing ANTHROPIC_API_KEY (see .env.example)")
            self._anthropic = anthropic.Anthropic(api_key=key)
        elif provider == "gemini":
            self._gemini_key = os.environ.get("GEMINI_API_KEY", "")
            if not self._gemini_key:
                raise SystemExit("Missing GEMINI_API_KEY (get a free one at aistudio.google.com)")
        else:
            raise SystemExit(f"Unknown llm.provider '{provider}' (use 'gemini' or 'anthropic')")

    def complete(self, model: str, system: str, prompt: str, max_tokens: int = 1500) -> str:
        if self.provider == "anthropic":
            msg = self._anthropic.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text

        # gemini — pace calls to respect free-tier rate limits
        wait = GEMINI_MIN_SECONDS_BETWEEN_CALLS - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        resp = requests.post(
            GEMINI_URL.format(model=model),
            params={"key": self._gemini_key},
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": max_tokens,
                    # Force strict JSON output (both our prompts expect JSON)
                    "responseMimeType": "application/json",
                    # Disable "thinking" so reasoning tokens don't eat the output
                    # budget and truncate the JSON mid-string
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=120,
        )
        self._last_call = time.time()
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc


def strip_fences(text: str) -> str:
    """Remove markdown code fences models sometimes wrap around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    return text.strip()
