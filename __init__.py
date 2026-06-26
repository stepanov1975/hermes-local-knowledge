"""Hermes plugin entry point for local_knowledge."""

try:  # Hermes loads plugin roots as package modules.
    from .hermes_local_knowledge.plugin import register
except ImportError:  # Pytest may import this file as a top-level module.
    from hermes_local_knowledge.plugin import register

__all__ = ["register"]
