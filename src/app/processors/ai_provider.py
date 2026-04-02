import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.app.config import settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "categorize.txt"


def _get_system_prompt() -> str:
    template = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else "Categorize messages. Return JSON."
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return template.replace("{current_datetime}", now)


class AIProvider(Protocol):
    async def categorize(self, messages: list[dict]) -> list[dict]: ...


class OpenAICompatibleProvider:
    """Works with OpenRouter, Groq, and any OpenAI-compatible API."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    @retry(wait=wait_exponential(min=1, max=60), stop=stop_after_attempt(3))
    async def categorize(self, messages: list[dict]) -> list[dict]:
        user_content = json.dumps({"messages": messages}, ensure_ascii=False)
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _get_system_prompt()},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        text = response.choices[0].message.content or "{}"
        parsed = json.loads(text)
        return parsed.get("results", [parsed] if "category" in parsed else [])


class GeminiProvider:
    """Google AI Studio via google-genai SDK."""

    def __init__(self, api_key: str, model: str):
        # Lazy import to avoid requiring google-genai when not used
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model

    @retry(wait=wait_exponential(min=1, max=60), stop=stop_after_attempt(3))
    async def categorize(self, messages: list[dict]) -> list[dict]:
        user_content = json.dumps({"messages": messages}, ensure_ascii=False)
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.client.models.generate_content(
                model=self.model,
                contents=f"{_get_system_prompt()}\n\n{user_content}",
                config={"response_mime_type": "application/json"},
            ),
        )
        text = response.text or "{}"
        parsed = json.loads(text)
        return parsed.get("results", [parsed] if "category" in parsed else [])


PROVIDER_MAP = {
    "openrouter": lambda model: OpenAICompatibleProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
        model=model,
    ),
    "groq": lambda model: OpenAICompatibleProvider(
        base_url="https://api.groq.com/openai/v1",
        api_key=settings.groq_api_key,
        model=model,
    ),
    "gemini": lambda model: GeminiProvider(
        api_key=settings.gemini_api_key,
        model=model,
    ),
}


def get_provider(name: str, model: str) -> AIProvider:
    factory = PROVIDER_MAP.get(name)
    if not factory:
        raise ValueError(f"Unknown AI provider: {name}. Available: {list(PROVIDER_MAP)}")
    return factory(model)


def get_primary_provider() -> AIProvider:
    return get_provider(settings.ai_provider, settings.ai_model)


def get_fallback_provider() -> AIProvider:
    return get_provider(settings.ai_fallback_provider, settings.ai_fallback_model)
