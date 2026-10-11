"""Gemini model adapter used by both LangGraph workflows."""
import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

API_KEY_VAR = "LLM_API_KEY"
MODEL_VAR = "LLM_MODEL"
FALLBACK_MODELS_VAR = "LLM_FALLBACK_MODELS"
PLACEHOLDER_KEY = "your-api-key-here"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FALLBACKS = ("gemini-3-flash-preview", "gemini-flash-lite-latest")
TIMEOUT_S = 20
MAX_RETRIES = 2
MAX_TOKENS = 4096


def load_api_key() -> str | None:
    load_dotenv()
    key = os.getenv(API_KEY_VAR, "").strip()
    return None if not key or key == PLACEHOLDER_KEY else key


def model_chain() -> tuple[str, ...]:
    load_dotenv()
    first = os.getenv(MODEL_VAR, "").strip() or DEFAULT_MODEL
    configured = tuple(x.strip() for x in os.getenv(FALLBACK_MODELS_VAR, "").split(",") if x.strip())
    return tuple(dict.fromkeys((first, *(configured or DEFAULT_FALLBACKS))))


class GeminiModels:
    """Small provider boundary around LangChain's Gemini integration.

    Tests provide a fake object with the same two methods; agent graphs never
    depend directly on a provider-specific response format.
    """

    def __init__(self, api_key: str, models: tuple[str, ...] | None = None):
        self.names = models or model_chain()
        self.models = [
            ChatGoogleGenerativeAI(
                model=name,
                api_key=api_key,
                temperature=0,
                max_tokens=MAX_TOKENS,
                timeout=TIMEOUT_S,
                max_retries=MAX_RETRIES,
            )
            for name in self.names
        ]

    def invoke_tools(self, system: str, messages: list[BaseMessage], tools: list[Any]):
        errors = []
        for name, model in zip(self.names, self.models):
            try:
                return model.bind_tools(tools).invoke([SystemMessage(content=system), *messages]), name
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        raise RuntimeError("all models failed (" + "; ".join(errors) + ")")

    def invoke_structured(self, system: str, messages: list[BaseMessage], schema: type[BaseModel]):
        errors = []
        for name, model in zip(self.names, self.models):
            try:
                runnable = model.with_structured_output(schema.model_json_schema(), method="json_schema")
                raw = runnable.invoke([SystemMessage(content=system), *messages])
                return schema.model_validate(raw), name
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        raise RuntimeError("all models failed (" + "; ".join(errors) + ")")
