"""Exercise UI setup and make sure passwords/uploads are never persisted."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.busch_jaeger_1095_bt.const import CONF_SECRET, CONF_UPLOAD, DOMAIN
from custom_components.busch_jaeger_1095_bt.protocol import AuthenticationError
from tests.test_credentials import export

ADDRESS = "AA:BB:CC:DD:EE:FF"
SECRET = "ab" * 32


async def start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )


@pytest.fixture
def mock_thermostat():
    with (
        patch("custom_components.busch_jaeger_1095_bt.config_flow.create_thermostat") as create,
        patch(
            "custom_components.busch_jaeger_1095_bt.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        create.return_value = AsyncMock()
        yield create


async def test_secret_setup(hass, mock_thermostat):
    result = await start(hass)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "secret"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: SECRET}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_SECRET: SECRET, "destination": 0x0201}
    await hass.async_block_till_done()


async def test_invalid_auth(hass, mock_thermostat):
    mock_thermostat.return_value.__aenter__.side_effect = AuthenticationError(
        "authentication failed"
    )
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "secret"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: SECRET}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_export_setup_does_not_store_password(hass, mock_thermostat, tmp_path):
    path = tmp_path / "installation.gepinstallation"
    path.write_bytes(export())

    @contextmanager
    def uploaded(hass, file_id):
        assert file_id == "01234567-89ab-cdef-0123-456789abcdef"
        try:
            yield path
        finally:
            path.unlink()

    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "installation"}
    )
    with patch(
        "custom_components.busch_jaeger_1095_bt.config_flow.process_uploaded_file", uploaded
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_UPLOAD: "01234567-89ab-cdef-0123-456789abcdef",
                CONF_PASSWORD: "synthetic-password",
            },
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_PASSWORD not in result["data"] and CONF_UPLOAD not in result["data"]
    assert len(result["data"][CONF_SECRET]) == 64
    assert not path.exists()
    await hass.async_block_till_done()


async def test_options_validate_range(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS, CONF_SECRET: SECRET}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"min_temp": 25, "max_temp": 20}
    )
    assert result["errors"] == {"base": "invalid_range"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"min_temp": 10, "max_temp": 30}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options == {"min_temp": 10, "max_temp": 30}


async def test_reauth_updates_existing_entry(hass, mock_thermostat):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Thermostat",
        data={CONF_ADDRESS: ADDRESS, CONF_SECRET: "cd" * 32},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "secret"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: SECRET}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SECRET] == SECRET
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    await hass.async_block_till_done()
