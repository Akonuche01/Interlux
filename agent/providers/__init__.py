"""Provider adapters."""

from .base import BaseProvider
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider
from .inception import InceptionProvider

PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "inception": InceptionProvider,
}
