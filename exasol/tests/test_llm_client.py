"""
tests/test_llm_client.py — agents/llm_client.call_tool() is the single
choke point every agent goes through to talk to Gemini, so it's tested
directly here with a mocked genai.Client rather than re-mocked inside
every agent's own test file.

Responses are built from the real google.genai.types classes (not bare
MagicMocks) so a schema change in the SDK would surface as a constructor
error here rather than silently passing against a fake shape.
"""

from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors, types

from agents.llm_client import call_tool, LLMCallError, LLMRateLimitError


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
def test_call_tool_returns_function_args_as_plain_dict(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _response_with_function_call(
        "run_query", {"sql": "SELECT 1", "explanation": "trivial"}
    )
    mock_client_cls.return_value = mock_client

    result = call_tool(
        api_key="fake-key",
        model="gemini-3.6-flash",
        system_prompt="system",
        tool_name="run_query",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
        user_content="how many documents are there?",
    )

    assert result == {"sql": "SELECT 1", "explanation": "trivial"}


@patch("agents.llm_client.genai.Client")
def test_call_tool_forces_the_named_function_only(mock_client_cls):
    """The whole point of this wrapper is that the model can't opt out of
    calling the tool — verify the request actually asks for that.
    """
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _response_with_function_call(
        "draft_action", {"email_subject": "x", "email_body": "y", "task_description": "z"}
    )
    mock_client_cls.return_value = mock_client

    call_tool(
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
def test_call_tool_raises_when_model_returns_no_function_call(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _response_with_no_function_call()
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMCallError):
        call_tool(
            api_key="fake-key",
            model="gemini-3.6-flash",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )


@patch("agents.llm_client.genai.Client")
def test_call_tool_raises_on_empty_candidates(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = types.GenerateContentResponse(candidates=[])
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMCallError):
        call_tool(
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
def test_call_tool_retries_on_429_then_succeeds(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        _quota_exceeded_error(),
        _response_with_function_call("run_query", {"sql": "SELECT 1", "explanation": "ok"}),
    ]
    mock_client_cls.return_value = mock_client

    result = call_tool(
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
def test_call_tool_raises_rate_limit_error_after_exhausting_retries(mock_client_cls, mock_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = _quota_exceeded_error("5s")
    mock_client_cls.return_value = mock_client

    with pytest.raises(LLMRateLimitError) as exc_info:
        call_tool(
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
def test_call_tool_does_not_retry_non_429_client_errors(mock_client_cls):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.ClientError(
        code=400, response_json={"error": {"code": 400, "message": "bad request"}}
    )
    mock_client_cls.return_value = mock_client

    with pytest.raises(errors.ClientError):
        call_tool(
            api_key="fake-key",
            model="gemini-3.6-flash",
            system_prompt="system",
            tool_name="run_query",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
            user_content="anything",
        )

    assert mock_client.models.generate_content.call_count == 1
