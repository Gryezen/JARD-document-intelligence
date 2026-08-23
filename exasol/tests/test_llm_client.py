"""
tests/test_llm_client.py — agents/llm_client.call_tool() is the single
choke point every agent goes through to talk to an LLM, so it's tested
directly here rather than re-mocked inside every agent's own test file.
Covers both backends: provider="ollama" (mocked ollama.Client) and
provider="gemini" (mocked genai.Client, using the real google.genai.types
classes so an SDK schema change surfaces here rather than silently
passing against a fake shape).
"""

import json
from unittest.mock import MagicMock, patch

import ollama
import pytest
from google.genai import errors, types

from agents.llm_client import (
    call_tool,
    LLMCallError,
    LLMConnectionError,
    LLMRateLimitError,
)


# --------------------------------------------------------------------------
# provider="gemini"
# --------------------------------------------------------------------------

def _response_with_function_call(name: str, args: dict):
    part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    content = types.Content(parts=[part])
    candidate = types.Candidate(content=content)
    return types.GenerateContentResponse(candidates=[candidate])


def _response_with_no_function_call():
    part = types.Part(text="I'd rather just explain in prose.")
    content = types.Content(parts=[part])
    candidate = types.Candidate(content=content)
    return types.GenerateContentResponse(candidates=[candidate])


@patch("agents.llm_client.genai.Client")
def test_gemini_returns_function_args_as_plain_dict(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _response_with_function_call(
        "run_query", {"sql": "SELECT 1", "explanation": "trivial"}
    )
    mock_client_cls.return_value = mock_client

    result = call_tool(
        provider="gemini",
        api_key="fake-key",
        model="gemini-3.6-flash",
        system_prompt="system",
        tool_name="run_query",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
        user_content="how many documents are there?",
    )

    assert result == {"sql": "SELECT 1", "explanation": "trivial"}


def test_gemini_without_api_key_raises_immediately():
    with pytest.raises(LLMCallError):
        call_tool(
            provider="gemini",
            api_key=None,
            model="gemini-3.6-flash",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )


@patch("agents.llm_client.genai.Client")
def test_gemini_forces_the_named_function_only(mock_client_cls):
    """The whole point of this wrapper is that the model can't opt out of
    calling the tool — verify the request actually asks for that.
    """
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _response_with_function_call(
        "draft_action", {"email_subject": "x", "email_body": "y", "task_description": "z"}
    )
    mock_client_cls.return_value = mock_client

    call_tool(
        provider="gemini",
        api_key="fake-key",
        model="gemini-3.6-flash",
        system_prompt="system",
        tool_name="draft_action",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
        user_content="draft it",
    )

    _, kwargs = mock_client.models.generate_content.call_args
    config = kwargs["config"]
    fcc = config.tool_config.function_calling_config
    assert fcc.mode == types.FunctionCallingConfigMode.ANY
    assert fcc.allowed_function_names == ["draft_action"]


@patch("agents.llm_client.genai.Client")
def test_gemini_raises_when_model_returns_no_function_call(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _response_with_no_function_call()
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMCallError):
        call_tool(
            provider="gemini",
            api_key="fake-key",
            model="gemini-3.6-flash",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )


@patch("agents.llm_client.genai.Client")
def test_gemini_raises_on_empty_candidates(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = types.GenerateContentResponse(candidates=[])
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMCallError):
        call_tool(
            provider="gemini",
            api_key="fake-key",
            model="gemini-3.6-flash",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )


def _quota_exceeded_error(retry_delay_seconds: str = "30s") -> errors.ClientError:
    """Build a ClientError shaped like Gemini's real 429 quota response
    (RESOURCE_EXHAUSTED with a RetryInfo.retryDelay), so retry tests exercise
    the same parsing path production traffic hits.
    """
    body = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": retry_delay_seconds,
                }
            ],
        }
    }
    return errors.ClientError(code=429, response_json=body)


@patch("agents.llm_client.time.sleep")
@patch("agents.llm_client.genai.Client")
def test_gemini_retries_on_429_then_succeeds(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        _quota_exceeded_error(),
        _response_with_function_call("run_query", {"sql": "SELECT 1", "explanation": "ok"}),
    ]
    mock_client_cls.return_value = mock_client

    result = call_tool(
        provider="gemini",
        api_key="fake-key",
        model="gemini-3.6-flash",
        system_prompt="system",
        tool_name="run_query",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
        user_content="how many documents are there?",
        max_retries=3,
    )

    assert result == {"sql": "SELECT 1", "explanation": "ok"}
    assert mock_client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()  # slept once, honoring the 30s retryDelay (+ jitter)
    assert mock_sleep.call_args[0][0] >= 30


@patch("agents.llm_client.time.sleep")
@patch("agents.llm_client.genai.Client")
def test_gemini_raises_rate_limit_error_after_exhausting_retries(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = _quota_exceeded_error("5s")
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMRateLimitError) as exc_info:
        call_tool(
            provider="gemini",
            api_key="fake-key",
            model="gemini-3.6-flash",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
            max_retries=2,
        )

    assert mock_client.models.generate_content.call_count == 3  # initial + 2 retries
    assert exc_info.value.retry_after_seconds == 5.0


@patch("agents.llm_client.genai.Client")
def test_gemini_does_not_retry_non_429_client_errors(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ClientError(
        code=400, response_json={"error": {"code": 400, "message": "bad request"}}
    )
    mock_client_cls.return_value = mock_client

    with pytest.raises(errors.ClientError):
        call_tool(
            provider="gemini",
            api_key="fake-key",
            model="gemini-3.6-flash",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )

    assert mock_client.models.generate_content.call_count == 1


# --------------------------------------------------------------------------
# provider="ollama"
# --------------------------------------------------------------------------

def _ollama_chat_response(payload: dict) -> dict:
    return {"message": {"content": json.dumps(payload)}}


@patch("agents.llm_client.ollama.Client")
def test_ollama_returns_parsed_json_as_plain_dict(mock_client_cls):
    mock_client = MagicMock()
    mock_client.chat.return_value = _ollama_chat_response({"sql": "SELECT 1", "explanation": "trivial"})
    mock_client_cls.return_value = mock_client

    result = call_tool(
        provider="ollama",
        model="qwen2.5:7b-instruct",
        ollama_host="http://localhost:11434",
        system_prompt="system",
        tool_name="run_query",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
        user_content="how many documents are there?",
    )

    assert result == {"sql": "SELECT 1", "explanation": "trivial"}
    mock_client_cls.assert_called_once_with(host="http://localhost:11434")


@patch("agents.llm_client.ollama.Client")
def test_ollama_passes_schema_as_format_constraint(mock_client_cls):
    mock_client = MagicMock()
    mock_client.chat.return_value = _ollama_chat_response({"related": True})
    mock_client_cls.return_value = mock_client
    schema = {"type": "object", "properties": {"related": {"type": "boolean"}}}

    call_tool(
        provider="ollama",
        model="qwen2.5:7b-instruct",
        system_prompt="system",
        tool_name="assess_relationship",
        tool_description="desc",
        tool_schema=schema,
        user_content="are these related?",
    )

    _, kwargs = mock_client.chat.call_args
    assert kwargs["format"] == schema
    assert kwargs["messages"][0] == {"role": "system", "content": "system"}
    assert kwargs["messages"][1] == {"role": "user", "content": "are these related?"}


@patch("agents.llm_client.ollama.Client")
def test_ollama_model_not_pulled_raises_connection_error_immediately(mock_client_cls):
    mock_client = MagicMock()
    mock_client.chat.side_effect = ollama.ResponseError("model 'qwen2.5:7b-instruct' not found", status_code=404)
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMConnectionError):
        call_tool(
            provider="ollama",
            model="qwen2.5:7b-instruct",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )

    # A missing model won't fix itself on retry — should fail on the first attempt.
    assert mock_client.chat.call_count == 1


@patch("agents.llm_client.ollama.Client")
def test_ollama_server_unreachable_raises_connection_error(mock_client_cls):
    mock_client = MagicMock()
    mock_client.chat.side_effect = ConnectionError("connection refused")
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMConnectionError):
        call_tool(
            provider="ollama",
            model="qwen2.5:7b-instruct",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )


@patch("agents.llm_client.time.sleep")
@patch("agents.llm_client.ollama.Client")
def test_ollama_retries_on_transient_response_error_then_succeeds(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.chat.side_effect = [
        ollama.ResponseError("model is still loading", status_code=503),
        _ollama_chat_response({"sql": "SELECT 1", "explanation": "ok"}),
    ]
    mock_client_cls.return_value = mock_client

    result = call_tool(
        provider="ollama",
        model="qwen2.5:7b-instruct",
        system_prompt="system",
        tool_name="run_query",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
        user_content="anything",
        max_retries=3,
    )

    assert result == {"sql": "SELECT 1", "explanation": "ok"}
    assert mock_client.chat.call_count == 2
    mock_sleep.assert_called_once()


@patch("agents.llm_client.time.sleep")
@patch("agents.llm_client.ollama.Client")
def test_ollama_raises_after_exhausting_retries_on_repeated_response_error(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.chat.side_effect = ollama.ResponseError("server busy", status_code=503)
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMCallError):
        call_tool(
            provider="ollama",
            model="qwen2.5:7b-instruct",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
            max_retries=2,
        )

    assert mock_client.chat.call_count == 3  # initial + 2 retries


@patch("agents.llm_client.time.sleep")
@patch("agents.llm_client.ollama.Client")
def test_ollama_retries_on_malformed_json_then_succeeds(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.chat.side_effect = [
        {"message": {"content": "not json at all"}},
        _ollama_chat_response({"sql": "SELECT 1", "explanation": "ok"}),
    ]
    mock_client_cls.return_value = mock_client

    result = call_tool(
        provider="ollama",
        model="qwen2.5:7b-instruct",
        system_prompt="system",
        tool_name="run_query",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
        user_content="anything",
        max_retries=3,
    )

    assert result == {"sql": "SELECT 1", "explanation": "ok"}
    assert mock_client.chat.call_count == 2


@patch("agents.llm_client.time.sleep")
@patch("agents.llm_client.ollama.Client")
def test_ollama_raises_after_exhausting_retries_on_repeated_malformed_json(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.chat.return_value = {"message": {"content": "still not json"}}
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMCallError):
        call_tool(
            provider="ollama",
            model="qwen2.5:7b-instruct",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
            max_retries=2,
        )

    assert mock_client.chat.call_count == 3


# --------------------------------------------------------------------------
# provider dispatch
# --------------------------------------------------------------------------

def test_unknown_provider_raises_value_error():
    with pytest.raises(ValueError):
        call_tool(
            provider="claude",
            model="whatever",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )
