"""Local OBS presentation preparation package."""

from __future__ import annotations


def main(*args, **kwargs):
    """Lazily import the application entry point to avoid importing optional runtime
    dependencies during a simple metadata or tooling import.
    """
    from .app import main as app_main

    return app_main(*args, **kwargs)


__all__ = ["main"]
