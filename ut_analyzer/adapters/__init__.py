"""Language adapters: one plugin per supported language behind a common interface."""

from .base import LanguageAdapter, AdapterRegistry, registry

__all__ = ["LanguageAdapter", "AdapterRegistry", "registry"]
