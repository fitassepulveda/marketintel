"""Provider-agnostic LLM client.

Set `llm.provider` in config/settings.yaml to "gemini" (free tier available)
or "anthropic". Both expose one method:

    client.complete(model, system, prompt, max_tokens) -> response text
"""
import json
import logging
import os
import time

import requests

log = logging.getLogger("llm_client")


class QuotaExhausted(RuntimeError):
    """Gemini returned a QUOTA-type 429 (daily cap, billing cap, or a key demoted to
    quota 0). Unlike a transient "model overloaded" 429, retrying cannot help until
    the quota resets or billing is fixed — callers should fail fast instead of
    burning ~30 minutes of backoff per run (see the 2026-07-15 incident)."""


def _quota_429(resp) -> bool:
    """Classify a 429: True = quota exhaustion (do NOT retry), False = transient.

    Google marks quota 429s with a QuotaFailure detail. Per-minute limits are worth
    retrying (backoff outlasts the window); per-day/billing limits and quotaValue 0
    (key demoted to free tier / suspended billing) are not.
    """
    if resp.status_code != 429:
        return False
    try:
        blob = json.dumps(resp.json()).lower()
    except ValueError:
        return False
    if '"quotavalue": "0"' in blob:
        return True   # zero quota: demoted/suspended key — hopeless today
    return "perday" in blob or "per_day" in blob or "daily" in blob

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
GEMINI_EMBED_MODEL = "gemini-embedding-001"
# Free tier allows 10 requests/minute on gemini-2.5-flash; pace ourselves to stay under.
GEMINI_MIN_SECONDS_BETWEEN_CALLS = 7
# Retries for transient overload responses (429/500/503), with exponential backoff.
# 6 attempts with backoff capped at 60s ride out a multi-minute Gemini overload.
GEMINI_MAX_RETRIES = 6
GEMINI_BACKOFF_CAP = 60


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

        # gemini — pace calls to respect free-tier rate limits, and retry the
        # transient overload codes (429/500/503) that the free tier returns often.
        gen_cfg = {
            "maxOutputTokens": max_tokens,
            # Force strict JSON output (both our prompts expect JSON)
            "responseMimeType": "application/json",
            # temperature 0 = deterministic: the same article gets the same relevance
            # score every run, so rankings are consistent rather than reshuffling.
            "temperature": 0,
        }
        # thinkingConfig only exists on 2.5-series models; sending it to others 400s.
        if "2.5" in model:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }
        resp = None
        for attempt in range(GEMINI_MAX_RETRIES):
            wait = GEMINI_MIN_SECONDS_BETWEEN_CALLS - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            resp = requests.post(
                GEMINI_URL.format(model=model),
                # Newer Google API keys (the "AQ." format) authenticate via this header,
                # not the legacy "?key=" query parameter.
                headers={"x-goog-api-key": self._gemini_key},
                json=payload,
                timeout=120,
            )
            self._last_call = time.time()
            if _quota_429(resp):
                raise QuotaExhausted(f"Gemini quota exhausted: {resp.text[:300]}")
            if resp.status_code in (429, 500, 503) and attempt < GEMINI_MAX_RETRIES - 1:
                backoff = min(GEMINI_BACKOFF_CAP, GEMINI_MIN_SECONDS_BETWEEN_CALLS * (2 ** attempt))
                log.warning("Gemini %s (overloaded); retry %d/%d in %ds",
                            resp.status_code, attempt + 1, GEMINI_MAX_RETRIES - 1, backoff)
                time.sleep(backoff)
                continue
            break
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc

    def web_research(self, model: str, system: str, prompt: str,
                     max_tokens: int = 800) -> tuple[str, list[dict]]:
        """Like complete(), but lets the model SEARCH THE LIVE WEB and returns
        (answer_text, web_sources). web_sources is [{"title","uri"}] from the
        grounding metadata — the real pages the model used.

        Gemini: enables Google Search grounding. Note grounding can't be combined
        with forced-JSON output, so callers should ask for JSON in the prompt and
        parse leniently. Anthropic (or no grounding): falls back to complete() with
        no web sources.
        """
        if self.provider != "gemini":
            return self.complete(model, system, prompt, max_tokens), []

        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],   # live web search grounding
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
        }
        resp = None
        for attempt in range(GEMINI_MAX_RETRIES):
            wait = GEMINI_MIN_SECONDS_BETWEEN_CALLS - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            resp = requests.post(
                GEMINI_URL.format(model=model),
                headers={"x-goog-api-key": self._gemini_key},
                json=payload, timeout=120,
            )
            self._last_call = time.time()
            if _quota_429(resp):
                raise QuotaExhausted(f"Gemini quota exhausted: {resp.text[:300]}")
            if resp.status_code in (429, 500, 503) and attempt < GEMINI_MAX_RETRIES - 1:
                backoff = min(GEMINI_BACKOFF_CAP, GEMINI_MIN_SECONDS_BETWEEN_CALLS * (2 ** attempt))
                log.warning("Gemini web_research %s (overloaded); retry %d/%d in %ds",
                            resp.status_code, attempt + 1, GEMINI_MAX_RETRIES - 1, backoff)
                time.sleep(backoff)
                continue
            break
        resp.raise_for_status()
        data = resp.json()
        try:
            cand = data["candidates"][0]
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc
        sources = []
        for chunk in cand.get("groundingMetadata", {}).get("groundingChunks", []) or []:
            web = chunk.get("web") or {}
            if web.get("uri"):
                sources.append({"title": web.get("title", ""), "uri": web["uri"]})
        return text, sources

    def embed(self, texts: list[str], model: str = GEMINI_EMBED_MODEL) -> list[list[float]]:
        """Return an embedding vector per input text (one batched Gemini call).

        Uses the Gemini embeddings API regardless of chat provider — it only needs a
        GEMINI_API_KEY. Retries the transient overload codes like complete() does.
        """
        key = self._gemini_key if self.provider == "gemini" else os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("GEMINI_API_KEY required for embeddings")
        body = {"requests": [
            {"model": f"models/{model}", "content": {"parts": [{"text": (t or "")[:2000]}]}}
            for t in texts
        ]}
        for attempt in range(GEMINI_MAX_RETRIES):
            resp = requests.post(
                GEMINI_EMBED_URL.format(model=model),
                headers={"x-goog-api-key": key}, json=body, timeout=120,
            )
            if _quota_429(resp):
                raise QuotaExhausted(f"Gemini quota exhausted: {resp.text[:300]}")
            if resp.status_code in (429, 500, 503) and attempt < GEMINI_MAX_RETRIES - 1:
                time.sleep(GEMINI_MIN_SECONDS_BETWEEN_CALLS * (2 ** attempt))
                continue
            break
        resp.raise_for_status()
        return [e["values"] for e in resp.json()["embeddings"]]


def strip_fences(text: str) -> str:
    """Remove markdown code fences models sometimes wrap around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    return text.strip()
