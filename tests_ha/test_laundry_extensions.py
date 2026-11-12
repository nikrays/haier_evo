"""The rebased features use the upstream device, catalogue and command path."""
from copy import deepcopy
from unittest.mock import Mock

from custom_components.haier_evo import api

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from . import test_laundry as base
from .test_laundry import WASHER, haier, ready, setup, wait_for


@pytest.fixture
def laundry(cloud):
    return base.laundry.__wrapped__(cloud)


def machine_entity(hass, entry, mac):
    cloud = haier(hass, entry)
    device = cloud.get_device_by_id(mac)
    return device, hass.states.get(device.machine_entity_id)


async def test_native_entities_and_modal_share_one_device(hass, laundry, make_entry, hass_storage):
    entry = await setup(hass, laundry, make_entry, hass_storage, control=True)
    machine, state = machine_entity(hass, entry, WASHER)
    assert state.entity_id.startswith("haier_evo.")
    assert state.attributes["friendly_name"] == machine.device_name
    assert len(state.attributes["program_catalog"]) == 5
    assert sum(len(g["programs"]) for g in state.attributes["program_catalog"]) == 48
    assert len(machine.programs) == 29
    registry = er.async_get(hass)
    root = registry.async_get(state.entity_id)
    controls = [e for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if e.domain in ("button", "select") and e.device_id == root.device_id]
    assert len(controls) == 5
    assert laundry.commands() == []


async def test_all_cloud_programs_can_stage_settings_without_commands(hass, laundry, make_entry, hass_storage):
    entry = await setup(hass, laundry, make_entry, hass_storage, control=True)
    for machine in haier(hass, entry).devices:
        for program in machine.programs:
            machine.set_selected_program(program["title"])
            settings = machine.program_settings
            assert not settings.get("error"), (machine.device_model, program["title"], settings)
    assert laundry.commands() == []


async def test_parameters_reach_the_existing_start_command(hass, laundry, make_entry, hass_storage):
    entry = await setup(hass, laundry, make_entry, hass_storage, control=True)
    machine, _ = machine_entity(hass, entry, WASHER)
    await ready(hass, laundry, machine, "FINISHED")
    await laundry.push(WASHER, {machine.codes["cyclePhase"]: "0"})
    await wait_for(lambda: machine.phase_value == "0", "the completed cycle to become idle")
    machine.set_selected_program("Смешанная")
    temperature = next(f for f in machine.program_settings["fields"] if "Температур" in f["title"])
    value = next(v["value"] for v in temperature["options"] if v["value"] != temperature["value"])
    await hass.services.async_call("haier_evo", "configure_program", {
        "entity_id": machine.machine_entity_id, "program": "Смешанная", "parameters": {temperature["key"]: value},
    }, blocking=True)
    assert laundry.commands() == []
    await hass.services.async_call("haier_evo", "execute_action", {
        "entity_id": machine.machine_entity_id, "action": "start",
    }, blocking=True)
    commands = laundry.commands(WASHER)
    assert len(commands) == 1
    assert {c["commandName"]: c["value"] for c in commands[0]["commands"]}[temperature["key"]] == value
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("haier_evo", "execute_action", {
            "entity_id": machine.machine_entity_id, "action": "start",
        }, blocking=True)
    assert len(laundry.commands(WASHER)) == 1


async def test_unknown_settings_and_active_edits_send_nothing(hass, laundry, make_entry, hass_storage):
    entry = await setup(hass, laundry, make_entry, hass_storage, control=True)
    machine, _ = machine_entity(hass, entry, WASHER)
    machine.set_selected_program("Смешанная")
    original = deepcopy(machine.program_settings)
    with pytest.raises(HomeAssistantError):
        machine.configure_program("Смешанная", {"invented": "1"})
    with pytest.raises(HomeAssistantError):
        machine.configure_program("Смешанная", {"50": "999"})
    assert machine.program_settings == original
    await ready(hass, laundry, machine, "IN_PROGRESS")
    with pytest.raises(HomeAssistantError):
        machine.configure_program("Смешанная", {"50": "30"})
    assert laundry.commands() == []


async def test_read_only_option_is_enforced_by_modal_service(hass, laundry, make_entry, hass_storage):
    entry = await setup(hass, laundry, make_entry, hass_storage, control=False)
    machine, _ = machine_entity(hass, entry, WASHER)
    assert machine.available_actions == []
    for action in ("start", "pause", "resume", "cancel"):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call("haier_evo", "execute_action", {
                "entity_id": machine.machine_entity_id, "action": action,
            }, blocking=True)
    assert laundry.commands() == []

@pytest.mark.parametrize("index,program_count,group_count", [(0,28,5),(1,29,5),(2,20,3)])
async def test_all_three_models_keep_valid_program_schemas(hass, index, program_count, group_count):
    data = base.LAUNDRY["devices"][index]
    cloud = Mock(hass=hass, cloud_available=True, laundry_control=True)
    machine = api.HaierLaundry(haier=cloud, device_mac=data["device_id"], backend_data=deepcopy(data["backend_data"]))
    assert len(machine.programs) == program_count
    assert len(machine.program_catalog) == group_count
    for program in machine.programs:
        machine.set_selected_program(program["title"])
        assert not machine.program_settings.get("error"), (data["device_name"], program["title"])
    cloud.send_command.assert_not_called()
    machine.events.close()

@pytest.mark.parametrize("text", [123, {"unexpected": True}, [], None])
async def test_malformed_alarm_text_does_not_break_setup_or_events(hass, text):
    data = deepcopy(base.HW90["backend_data"])
    control = data["smartDeviceControl"]
    control["alarms"] = ["badAlarm"]
    control["errors"] = {"alarmToResult": {"badAlarm": {"text": text}}}
    cloud = Mock(hass=hass, cloud_available=True, laundry_control=True)
    machine = api.HaierLaundry(haier=cloud, device_mac=WASHER, backend_data=data)
    assert isinstance(machine.laundry_errors[0]["text"], str)
    assert machine.laundry_errors[0]["code"] == "badAlarm"
    machine.events.observe()
    machine.events.close()


async def test_new_phase_clears_a_terminal_status_when_start_event_was_lost(hass):
    cloud = Mock(hass=hass, cloud_available=True, laundry_control=True)
    machine = api.HaierLaundry(haier=cloud, device_mac=WASHER, backend_data=deepcopy(base.HW90["backend_data"]))
    machine.program_status = "FINISHED"
    machine._values[machine.codes["onOffStatus"]] = "1"
    machine._values[machine.codes["cyclePhase"]] = "0"
    machine.on_message({"event": "status", "payload": {"statuses": [{"properties": {machine.codes["cyclePhase"]: "3"}}]}})
    assert machine.state == "running"
    assert machine.program_status is None
    machine.events.close()


async def test_care_keeps_native_pause_resume_and_cancel(hass):
    cloud = Mock(hass=hass, cloud_available=True, laundry_control=True)
    data = deepcopy(base.HW90["backend_data"])
    machine = api.HaierLaundry(haier=cloud, device_mac=WASHER, backend_data=data)
    care = api.laundry_screen(data)["freshAirSystem"]["freshAirSystemAttrValue"]
    machine._values[machine.codes["onOffStatus"]] = "1"
    machine._values[machine.codes["remoteCtrValid"]] = "1"
    machine._values[machine.codes["cyclePhase"]] = str(care)
    machine.program_status = "IN_PROGRESS"
    assert machine.state == "care"
    assert machine.available_actions == ["pause", "cancel"]
    machine.program_status = "PAUSED"
    assert machine.state == "paused"
    assert machine.available_actions == ["resume", "cancel"]
    machine.events.close()

@pytest.mark.parametrize("phases,expected", [(["0","3"],"running"),(["3","0"],"idle")])
async def test_batched_status_uses_the_last_phase(hass, phases, expected):
    cloud = Mock(hass=hass, cloud_available=True, laundry_control=True)
    machine = api.HaierLaundry(haier=cloud, device_mac=WASHER, backend_data=deepcopy(base.HW90["backend_data"]))
    machine.program_status = "IN_PROGRESS"
    machine._values[machine.codes["onOffStatus"]] = "1"
    machine._values[machine.codes["cyclePhase"]] = "3"
    machine.on_message({"event": "status", "payload": {"statuses": [
        {"properties": {machine.codes["cyclePhase"]: phase}} for phase in phases]}})
    assert machine.state == expected
    machine.events.close()


async def test_standalone_start_releases_previous_idle_override(hass):
    cloud = Mock(hass=hass, cloud_available=True, laundry_control=True)
    machine = api.HaierLaundry(haier=cloud, device_mac=WASHER, backend_data=deepcopy(base.HW90["backend_data"]))
    machine.program_status = "IN_PROGRESS"
    machine._values[machine.codes["onOffStatus"]] = "1"
    machine._phase_idle = True
    assert machine.state == "idle"
    machine.on_message({"event": "ProgramWasherBaseEvent", "data": {"status": "PROGRAM_STARTED"}})
    assert machine.state == "running"
    machine.events.close()
