"""Standardized offline research library; no import-time registration or execution."""
from .catalog import DEFINITIONS, EXISTING, BY_KEY
from .runtime import ComposedResearchRuntime

__all__ = ['DEFINITIONS','EXISTING','BY_KEY','ComposedResearchRuntime']
