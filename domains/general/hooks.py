"""
General domain hook — defaults only; override points reserved for future use.
"""

from src.hooks.base import DomainHook


class GeneralDomainHook(DomainHook):
    """General-purpose domain: rely on base `DomainHook` defaults."""
