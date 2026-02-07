import asyncio
import json
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    CLAUDE = "claude"
    OPENAI = "openai"
    OLLAMA = "ollama"


class LLMClient:
    def __init__(self, provider: LLMProvider, api_key: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None):
        self.provider = provider
        self.api_key = api_key
        self.model = model or self._default_model()
        self.base_url = base_url
        self._client = None

    def _default_model(self) -> str:
        if self.provider == LLMProvider.CLAUDE:
            return "claude-sonnet-4-20250514"
        if self.provider == LLMProvider.OLLAMA:
            return "qwen2.5-coder:7b"
        return "gpt-4o-mini"

    def _get_client(self):
        if self._client is None:
            if self.provider == LLMProvider.CLAUDE:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
            elif self.provider == LLMProvider.OPENAI:
                import openai
                self._client = openai.AsyncOpenAI(api_key=self.api_key)
            elif self.provider == LLMProvider.OLLAMA:
                import httpx
                self._client = httpx.AsyncClient(base_url=self.base_url or "http://localhost:11434", timeout=120.0)
        return self._client

    async def complete(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> str:
        """Send a completion request and return the response text."""
        client = self._get_client()

        for attempt in range(3):
            try:
                if self.provider == LLMProvider.CLAUDE:
                    kwargs = {
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                    if system:
                        kwargs["system"] = system
                    response = await client.messages.create(**kwargs)
                    return response.content[0].text

                elif self.provider == LLMProvider.OPENAI:
                    messages = []
                    if system:
                        messages.append({"role": "system", "content": system})
                    messages.append({"role": "user", "content": prompt})
                    response = await client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    return response.choices[0].message.content
                
                elif self.provider == LLMProvider.OLLAMA:
                    messages = []
                    if system:
                        messages.append({"role": "system", "content": system})
                    messages.append({"role": "user", "content": prompt})
                    
                    response = await client.post("/api/chat", json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                        }
                    })
                    response.raise_for_status()
                    return response.json()["message"]["content"]

            except Exception as e:
                logger.warning(f"LLM request failed (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

    async def complete_json(
        self,
        prompt: str,
        system: Optional[str] = None,
    ) -> dict:
        """Complete and parse response as JSON."""
        json_system = (system or "") + "\n\nRespond with valid JSON only. No markdown, no explanation."
        text = await self.complete(prompt, system=json_system.strip())

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Retry once
            logger.warning("JSON parse failed, retrying...")
            text = await self.complete(
                prompt + "\n\nIMPORTANT: Return ONLY valid JSON.",
                system=json_system,
            )
            text = text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)
            return json.loads(text)


def get_llm_client() -> Optional[LLMClient]:
    """Retrieve LLM client based on application settings."""
    from job_search.config import settings

    provider_str = settings.llm_provider.lower()
    
    if provider_str == "ollama":
        return LLMClient(
            provider=LLMProvider.OLLAMA,
            model=settings.llm_model or "qwen2.5-coder:7b",
            base_url=settings.ollama_base_url
        )

    api_key = settings.anthropic_api_key if provider_str == "claude" else settings.openai_api_key

    if not api_key:
        logger.warning(f"No API key found for LLM provider: {provider_str}")
        return None

    return LLMClient(
        provider=LLMProvider.CLAUDE if provider_str == "claude" else LLMProvider.OPENAI,
        api_key=api_key,
        model=settings.llm_model,
    )
