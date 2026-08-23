"""
agents/llm_client.py — one shared wrapper around forced structured-output
calling, used by every agent that needs structured (never free-text) model
output: extraction, reasoning, action, relationships, and chat's SQL
generation.

Two backends are supported, selected per-call via `provider`:
  - "ollama": local models served by Ollama (https://ollama.com), run on
    your own GPU. Free, no quota, no external network call. Uses Ollama's
    structured-output support (`format=<json schema>`) to force the
    response to match the tool's schema — this works with any local model,
    not just ones with native tool-calling support.
  - "gemini": Google's hosted API, forced function calling via
    FunctionCallingConfigMode.ANY. Kept intact for whichever slot (if any)
    you still want on a hosted model; see config.py's *_PROVIDER env vars.

Each agent file keeps the same shape it had before this ever changed —
build a JSON-schema tool, call one function, get back a plain dict —
regardless of which backend is actually answering.
"""

import json
import random
import time

import ollama
from google import genai
from google.genai import errors, types


class LLMCallError(Exception):
    """Raised when the model doesn't return the forced structured output at
    all (e.g. safety block, empty candidates, malformed JSON). Mirrors the
    old 'model did not return a tool_use block' failure mode so callers
    don't need to change their error handling.
    """


class LLMRateLimitError(LLMCallError):
    """Raised when a *hosted* call (Gemini) still fails with HTTP 429 after
    exhausting retries. Kept as a distinct subclass (rather than reusing
    LLMCallError) so callers/routes can tell "the model refused to call the
    tool" apart from "we're out of quota" and react differently — e.g.
    return 429 with a Retry-After to the frontend instead of a flat 500, or
    mark a document 'failed' with a message that makes clear a re-run later
    would work.

    retry_after_seconds is the server-suggested wait, when Gemini's 429
    body included a RetryInfo (it usually does) — None if it didn't.
    Not applicable to Ollama, which has no quota to exhaust.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LLMConnectionError(LLMCallError):
    """Raised for Ollama-specific setup problems: the server isn't running,
    or the requested model hasn't been pulled locally. Distinct from
    LLMCallError so routes can surface a "run `ollama pull <model>`" style
    message instead of a generic failure.
    """


def _extract_retry_delay(exc: "errors.ClientError") -> float | None:
    """Pull the server-suggested retry delay out of a 429's RetryInfo, if
    present. Gemini's free-tier quota error includes one (see the
    'retryDelay': '30s' field in the RetryInfo detail) — honoring it is
    friendlier than guessing a backoff blind.
    """
    details = getattr(exc, "details", None) or {}
    for item in details.get("error", {}).get("details", []) or []:
        if item.get("@type", "").endswith("RetryInfo"):
            raw = item.get("retryDelay", "")
            if raw.endswith("s"):
                try:
                    return float(raw[:-1])
                except ValueError:
                    return None
    return None


def call_tool(
    provider: str,
    model: str,
    system_prompt: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict,
    user_content: str,
    api_key: str | None = None,
    ollama_host: str = "http://localhost:11434",
    max_output_tokens: int = 4096,
    max_retries: int = 3,
    base_backoff_seconds: float = 2.0,
) -> dict:
    """Force the model to produce exactly the tool's schema and return it
    as a plain dict, regardless of which backend answers.

    provider="ollama" (default everywhere via config.py) runs entirely
    locally against an Ollama server — no API key, no quota, no network
    call leaves the machine.

    provider="gemini" uses Google's hosted forced function calling and
    requires api_key; see _call_tool_gemini for its retry/quota behavior.
    """
    if provider == "ollama":
        return _call_tool_ollama(
            host=ollama_host,
            model=model,
            system_prompt=system_prompt,
            tool_schema=tool_schema,
            user_content=user_content,
            max_retries=max_retries,
            base_backoff_seconds=base_backoff_seconds,
        )
    if provider == "gemini":
        if not api_key:
            raise LLMCallError(
                "provider='gemini' requires GEMINI_API_KEY to be set "
                "(see config.py / .env.example)."
            )
        return _call_tool_gemini(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            tool_name=tool_name,
            tool_description=tool_description,
            tool_schema=tool_schema,
            user_content=user_content,
            max_output_tokens=max_output_tokens,
            max_retries=max_retries,
            base_backoff_seconds=base_backoff_seconds,
        )
    raise ValueError(f"Unknown LLM provider {provider!r}: expected 'ollama' or 'gemini'")


def _call_tool_ollama(
    host: str,
    model: str,
    system_prompt: str,
    tool_schema: dict,
    user_content: str,
    max_retries: int = 3,
    base_backoff_seconds: float = 2.0,
) -> dict:
    """Force structured output matching tool_schema using Ollama's native
    JSON-schema-constrained generation (`format=<schema>`). This works with
    any model Ollama can run — it doesn't require the model to have
    explicit tool-calling support, since the constraint is applied at the
    decoding level rather than relying on the model choosing to call a
    function.

    Retries on transient failures (e.g. the server momentarily busy loading
    a model) with exponential backoff. Connection failures and "model not
    pulled" are raised immediately as LLMConnectionError since retrying
    won't fix either — the fix is starting Ollama or running `ollama pull`.
    """
    client = ollama.Client(host=host)
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                format=tool_schema,
                options={"temperature": 0},
            )
        except ollama.ResponseError as exc:
            status = getattr(exc, "status_code", None)
            message = str(exc)
            if status == 404 or "not found" in message.lower():
                raise LLMConnectionError(
                    f"Model '{model}' isn't available on this Ollama server. "
                    f"Pull it first: ollama pull {model}"
                ) from exc
            last_exc = exc
            if attempt == max_retries:
                raise LLMCallError(
                    f"Ollama call failed after {max_retries} retries: {message}"
                ) from exc
            time.sleep(base_backoff_seconds * (2 ** attempt) + random.uniform(0, 1))
            continue
        except (ConnectionError, OSError) as exc:
            raise LLMConnectionError(
                f"Couldn't reach Ollama at {host}. Is it running? Try: ollama serve"
            ) from exc

        content = response["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last_exc = exc
            if attempt == max_retries:
                raise LLMCallError(
                    f"Ollama returned output that didn't match the requested "
                    f"schema after {max_retries} retries: {content[:300]!r}"
                ) from exc
            time.sleep(base_backoff_seconds * (2 ** attempt) + random.uniform(0, 1))

    raise LLMCallError(f"Ollama call failed: {last_exc}")


def _call_tool_gemini(
    api_key: str,
    model: str,
    system_prompt: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict,
    user_content: str,
    max_output_tokens: int = 4096,
    max_retries: int = 3,
    base_backoff_seconds: float = 2.0,
) -> dict:
    """Force the model to call exactly one named tool and return its
    arguments as a plain dict, via Gemini's hosted API.

    Retries on HTTP 429 (rate limit / quota exceeded) with exponential
    backoff + jitter, honoring the server's suggested retryDelay when the
    error body includes one. This smooths over short bursts and per-minute
    throttling. It does NOT solve a fully exhausted *daily* free-tier quota
    (e.g. Gemini's 20-requests/day free tier) — no amount of local retrying
    manufactures more quota, so once the daily cap is hit every retry in
    this loop will also fail and the call still raises LLMRateLimitError.
    The real fix for that is enabling billing on the API key (paid tier
    quotas are far higher) or cutting the number of calls made per document
    — or, as of this project's Ollama wiring, simply not routing that slot
    through Gemini at all.
    """
    client = genai.Client(api_key=api_key)

    function_declaration = types.FunctionDeclaration(
        name=tool_name,
        description=tool_description,
        parameters_json_schema=tool_schema,
    )

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=max_output_tokens,
        tools=[types.Tool(function_declarations=[function_declaration])],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY,
                allowed_function_names=[tool_name],
            )
        ),
    )

    last_retry_after = None
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model, contents=user_content, config=config
            )
            break
        except errors.ClientError as exc:
            if exc.code != 429:
                raise
            last_retry_after = _extract_retry_delay(exc)
            if attempt == max_retries:
                raise LLMRateLimitError(
                    f"Rate limited by Gemini after {max_retries} retries: {exc.message}",
                    retry_after_seconds=last_retry_after,
                ) from exc
            wait = last_retry_after if last_retry_after is not None else base_backoff_seconds * (2 ** attempt)
            wait += random.uniform(0, 1)  # jitter, avoid retry storms if several docs 429 at once
            time.sleep(wait)
    else:
        # Unreachable (loop always breaks or raises) but keeps type checkers happy.
        raise LLMRateLimitError("Rate limited by Gemini", retry_after_seconds=last_retry_after)

    candidates = response.candidates or []
    for candidate in candidates:
        parts = candidate.content.parts if candidate.content else []
        for part in parts:
            if part.function_call is not None and part.function_call.name == tool_name:
                # function_call.args is already a plain dict (google-genai
                # decodes the protobuf Struct for you).
                return dict(part.function_call.args)

    raise LLMCallError(f"Model did not return a call to '{tool_name}'")
