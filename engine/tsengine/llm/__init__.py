"""LLM access layer. Every model call in the engine goes through here, and
every call targets OpenRouter (OpenAI-compatible). The router picks a model per
task from config; the client speaks the wire protocol."""
from .router import ModelRouter, LLMResult
from .client import OpenRouterClient, OpenRouterError

__all__ = ["ModelRouter", "LLMResult", "OpenRouterClient", "OpenRouterError"]
