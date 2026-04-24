"""Conversion domain hook — defaults only."""

from src.hooks.base import DomainHook


class ConversionDomainHook(DomainHook):
    """Conversion / companion domain: base DomainHook defaults."""

    def __init__(self, config=None):
        self._config = config
