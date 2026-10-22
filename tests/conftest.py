"""Enable the custom integration and prevent real USB/Bluetooth hardware access."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.usb import USBDiscovery

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations, mock_bluetooth):
    with patch.object(USBDiscovery, "_async_start_monitor", new=AsyncMock(), create=True):
        yield
