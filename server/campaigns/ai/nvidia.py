"""NVIDIA-hosted models (Nemotron) for the drafting assistant, through NVIDIA's OpenAI-compatible chat API
(default https://integrate.api.nvidia.com/v1). Standard library only: no extra dependency.

`create()` returns an object shaped like the Anthropic response the rest of drafting.py already understands (content text blocks,
usage, stop_reason, model, _request_id), so validation, privacy checks, telemetry and metrics are identical for both providers.
The exception class names match what drafting._friendly() turns into a safe message for the page. The key is never put in a message.
"""
import json
import re
import socket
import urllib.error
import urllib.request
from types import SimpleNamespace

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1"
MAX_TOKENS = 4000
TEMPERATURE = 0.5
JSON_INSTRUCTION = ("\n\nReply with ONLY one JSON object, no other text, no markdown fences. Its keys: \"subject\" (string), \"body\" (string), "
                    "\"used_context_ids\" (array of strings: the ids of the context items you actually used).")


class _ApiError(Exception):
    pass


class AuthenticationError(_ApiError):
    pass


class PermissionDeniedError(_ApiError):
    pass


class NotFoundError(_ApiError):
    pass


class RateLimitError(_ApiError):
    pass


class BadRequestError(_ApiError):
    pass


class APITimeoutError(_ApiError):
    pass


class APIConnectionError(_ApiError):
    pass


class APIStatusError(_ApiError):
    pass


def _post(url, headers, body, timeout):
    """-> (status, headers, bytes). Network failures raise the classes above. Patched in tests."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read()
    except (socket.timeout, TimeoutError):
        raise APITimeoutError("timeout")
    except urllib.error.URLError as err:
        if isinstance(getattr(err, "reason", None), (socket.timeout, TimeoutError)):
            raise APITimeoutError("timeout")
        raise APIConnectionError("connection")


def _raise_for(status):
    if status == 401:
        raise AuthenticationError("401")
    if status == 403:
        raise PermissionDeniedError("403")
    if status == 404:
        raise NotFoundError("404")
    if status == 429:
        raise RateLimitError("429")
    if status in (400, 422):
        raise BadRequestError(str(status))
    raise APIStatusError(str(status))


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_json_text(text):
    """Nemotron may wrap the answer in <think>...</think> or ``` fences. Return the JSON object text, or the cleaned text if none found."""
    text = _THINK.sub("", text or "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text.strip()


def create(model, system, user, api_key, base_url=None, timeout=60.0):
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if not base.startswith("https://"):
        raise APIConnectionError("the NVIDIA base URL must be https")
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json", "Accept": "application/json"}
    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE, "stream": False}
    status, hdrs, raw = _post(base + "/chat/completions", headers, dict(body, response_format={"type": "json_object"}), timeout)
    if status in (400, 422):                                    # some hosted models do not accept response_format: ask again without it
        status, hdrs, raw = _post(base + "/chat/completions", headers, body, timeout)
    if status != 200:
        _raise_for(status)
    try:
        data = json.loads(raw.decode("utf-8"))
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise APIStatusError("bad reply")
    finish = choice.get("finish_reason")
    stop = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(finish, finish or "end_turn")
    usage = data.get("usage") or {}
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=extract_json_text(text))],
        usage=SimpleNamespace(input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                              cache_read_input_tokens=None, iterations=None),
        stop_reason=stop, model=data.get("model") or model,
        _request_id=hdrs.get("nvcf-reqid") or hdrs.get("x-request-id") or data.get("id"))
