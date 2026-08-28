"""Pytest configuration for libtmux-easy tests."""
import pytest

# Disable libtmux's built-in pytest plugin which conflicts with newer pytest
pytest_plugins = []
