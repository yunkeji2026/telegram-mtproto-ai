"""
Domain Hook system — event-driven extension points for domain packs.

Core engine calls hooks at key processing stages; domain packs implement
domain-specific logic (e.g. payment channel handling) in their own hooks.py.
"""

from src.hooks.base import DomainHook, HookContext
from src.hooks.registry import HookRegistry

__all__ = ["DomainHook", "HookContext", "HookRegistry"]
