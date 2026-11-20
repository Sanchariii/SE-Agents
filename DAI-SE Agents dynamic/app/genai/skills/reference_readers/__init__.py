"""Bounded, style-only readers for skill reference assets."""

from app.genai.skills.reference_readers.dispatch import inspect_file, inspect_website

__all__ = ["inspect_file", "inspect_website"]
