"""
Small AI provider adapter for Anthropic Claude and Google Gemini.
Keeps cost controls in config while letting the rest of the agent call one API.
"""

import asyncio
import json
import os
import re
from typing import Any

import anthropic
import httpx
from google import genai
from google.genai import types


_OLLAMA_BASE = "http://localhost:11434/v1"


def provider_name(ai_config: dict) -> str:
    return str(ai_config.get("provider") or "anthropic").strip().lower()


def _ollama_available() -> bool:
    """Quick check whether local Ollama is reachable."""
    try:
        resp = httpx.get(f"{_OLLAMA_BASE}/models", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


def has_required_api_key(ai_config: dict) -> bool:
    provider = provider_name(ai_config)
    if provider == "ollama":
        return True  # no key required for local Ollama
    if provider == "gemini":
        return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    if provider == "openai":
        base_url = str(os.getenv("OPENAI_BASE_URL") or "").lower()
        if "localhost" in base_url or "127.0.0.1" in base_url:
            return True
        return bool(os.getenv("OPENAI_API_KEY"))
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def missing_api_key_message(ai_config: dict) -> str:
    provider = provider_name(ai_config)
    if provider == "gemini":
        return "GEMINI_API_KEY is not set. Add a Google AI Studio API key to .env."
    if provider == "openai":
        return "OPENAI_API_KEY is not set. Add it to .env, or route OPENAI_BASE_URL to local 9router/ollama."
    return "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."


def _extract_gemini_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    try:
        parts = response.candidates[0].content.parts
        return "".join(getattr(part, "text", "") for part in parts).strip()
    except Exception:
        return ""


def _request_timeout_seconds(ai_config: dict) -> float:
    return float(ai_config.get("request_timeout_seconds", 60))


async def generate_text(
    *,
    ai_config: dict,
    system_prompt: str,
    prompt: str,
    max_tokens: int,
    json_mode: bool = False,
) -> str:
    provider = provider_name(ai_config)
    model = ai_config["model"]
    timeout_seconds = _request_timeout_seconds(ai_config)

    if provider == "ollama":
        # Local Ollama — free, no API key required.
        def _call_ollama() -> str:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": ai_config.get("temperature", 0.1),
                "stream": False,
            }
            if json_mode:
                payload["format"] = "json"
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    f"{_OLLAMA_BASE}/chat/completions",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"].strip()

        return await asyncio.wait_for(
            asyncio.to_thread(_call_ollama),
            timeout=timeout_seconds + 5,
        )

    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )
        config_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "max_output_tokens": max_tokens,
            "temperature": ai_config.get("temperature", 0.2),
        }
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        def _call_gemini() -> str:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            return _extract_gemini_text(response).strip()

        return await asyncio.wait_for(
            asyncio.to_thread(_call_gemini),
            timeout=timeout_seconds + 5,
        )

    if provider == "openai":
        base_url = os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key and base_url and (
            "localhost" in base_url.lower() or "127.0.0.1" in base_url.lower()
        ):
            # 9router local deployments may accept any bearer token.
            api_key = "test-token"

        def _call_openai() -> str:
            if not base_url:
                raise ValueError("OPENAI_BASE_URL is not set.")

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key or ''}",
            }

            # Use non-streaming for JSON mode — streaming causes truncation
            # when the response is parsed incrementally and the stream cuts short.
            use_stream = not json_mode
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": ai_config.get("temperature", 0.2),
                "stream": use_stream,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            if not use_stream:
                with httpx.Client(timeout=timeout_seconds) as client:
                    response = client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    return data["choices"][0]["message"]["content"].strip()

            content_parts: list[str] = []
            reasoning_parts: list[str] = []

            with httpx.Client(timeout=timeout_seconds) as client:
                with client.stream(
                    "POST",
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    for raw_line in response.iter_lines():
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        reasoning = delta.get("reasoning_content")
                        if content:
                            content_parts.append(str(content))
                        if reasoning:
                            reasoning_parts.append(str(reasoning))

            text = "".join(content_parts).strip()
            if text:
                return text
            # Some reasoning-heavy models can return only reasoning tokens.
            return "".join(reasoning_parts).strip()

        return await asyncio.wait_for(
            asyncio.to_thread(_call_openai),
            timeout=timeout_seconds + 5,
        )

    client = anthropic.Anthropic(timeout=timeout_seconds)

    def _call_anthropic() -> str:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    return await asyncio.wait_for(
        asyncio.to_thread(_call_anthropic),
        timeout=timeout_seconds + 5,
    )


def parse_json_response(raw: str) -> dict:
    text = (raw or "").strip()
    # Strip markdown code fences regardless of spacing or language tag.
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # Direct parse (ideal path).
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Streaming truncation recovery: find the outermost JSON object and try to
    # parse just that substring, or close any open brace depth.
    obj_start = text.find("{")
    if obj_start != -1:
        fragment = text[obj_start:]
        depth = 0
        last_close = -1
        for i, ch in enumerate(fragment):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    last_close = i + 1
                    break

        # Try the cleanly-closed substring first.
        if last_close != -1:
            try:
                return json.loads(fragment[:last_close])
            except json.JSONDecodeError:
                pass

        # Repair: close any open string then append missing closing braces.
        repaired = fragment
        if repaired.count('"') % 2 != 0:
            repaired += '"'
        repaired += "}" * max(0, repaired.count("{") - repaired.count("}"))
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot parse JSON from model response: {text[:300]!r}")
