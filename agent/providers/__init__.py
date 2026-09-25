"""Provider adapters."""

from .base import BaseProvider
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider
from .inception import InceptionProvider
from .tokenharbor import TokenHarborProvider
from .local import LocalProvider

PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "inception": InceptionProvider,
    "tokenharbor": TokenHarborProvider,
    "local": LocalProvider,
}
