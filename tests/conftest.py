"""Test fixtures for the device watchdog blueprint."""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Blueprints are core automations, but the fixture keeps HA's loader happy."""
    return
