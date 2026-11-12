"""Read-only background recovery on a real HA core, independent of the frontend."""
from __future__ import annotations

import copy
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from requests import Response
from requests.exceptions import HTTPError

from custom_components.haier_evo import api
from custom_components.haier_evo.laundry_events import EVENT_TYPE
from .conftest import haier
from .test_laundry import DRYER, HD80, HW90, LAUNDRY, WASHER, setup


@pytest.fixture
def laundry(cloud):
    cloud.home = copy.deepcopy(LAUNDRY["backend_data"])
    cloud.descriptions = {d["device_id"]: copy.deepcopy(d["backend_data"]) for d in (HW90, HD80)}
    return cloud


@pytest.fixture
async def machines(hass, laundry, make_entry, hass_storage):
    entry = await setup(hass, laundry, make_entry, hass_storage)
    cloud = haier(hass, entry)
    yield entry, cloud, [cloud.get_device_by_id(mac) for mac in (WASHER, DRYER)]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def snapshot(device, *, status="IN_PROGRESS", phase="2", minutes=17):
    data = copy.deepcopy(device.status_data)
    data["program"] = {"title": "Mixed", "status": status}
    attrs = api.device_part(data)["attributes"]
    values = {device.codes.get("cyclePhase"): phase, device.codes.get("onOffStatus"): "1",
              device.codes.get("remainingTimeHH"): "0", device.codes.get("remainingTimeMM"): str(minutes),
              device.codes.get("remainingDryTime"): str(minutes)}
    for attribute in attrs:
        code = str(attribute.get("name"))
        if code in values:
            attribute["currentValue"] = values[code]
    return data


async def running(hass, device):
    device.update_from_backend(snapshot(device))
    await hass.async_block_till_done()
    assert device.state == "running"


@pytest.mark.parametrize(("status", "connected", "seconds"), [
    ("IN_PROGRESS", True, 30), ("PAUSED", True, 30), ("FINISHED", True, 120),
    ("FINISHED", False, 60), ("IN_PROGRESS", False, 60),
])
async def test_interval_tracks_machine_state(hass, machines, status, connected, seconds):
    device = machines[2][0]
    device.update_from_backend(snapshot(device, status=status))
    device.available = connected
    await hass.async_block_till_done()
    assert device.recovery.normal_interval == timedelta(seconds=seconds)


async def test_postcare_keeps_active_recovery_interval(hass, machines):
    device = machines[2][0]
    screen = api.laundry_screen(device.status_data)
    care = screen.get("freshAirSystem") or (screen.get("phase") or {}).get("antiCrease") or {}
    phase = care.get("freshAirSystemAttrValue", care.get("cyclePhaseValue"))
    assert phase is not None, "fixture contains the app's postcare phase"
    device.update_from_backend(snapshot(device, phase=str(phase)))
    await hass.async_block_till_done()
    assert device.in_postcare
    assert device.recovery.normal_interval == timedelta(seconds=30)


async def test_background_timer_polls_without_frontend(hass, machines, monkeypatch):
    _, cloud, devices = machines
    device = devices[0]
    await running(hass, device)
    device._last_status_observed = time.monotonic() - 100
    response = snapshot(device, minutes=12)
    pull = Mock(return_value=response)
    monkeypatch.setattr(cloud, "pull_device_data", pull)
    assert device.recovery._listeners, "setup owns a listener even when nobody opens More Info"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    pull.assert_called_once_with(device.device_id)
    assert device.remaining_time == 12
    assert device.program_title == "Mixed"


async def test_recent_timer_push_skips_poll_but_alarm_noise_does_not(hass, machines, monkeypatch):
    _, cloud, devices = machines
    device = devices[0]
    await running(hass, device)
    pull = Mock(return_value=snapshot(device, minutes=11))
    monkeypatch.setattr(cloud, "pull_device_data", pull)
    await device.recovery._async_update_data()
    assert not pull.called
    previous = time.monotonic() - 100
    device._last_status_observed = previous
    device.on_message({"event": "alarms", "payload": {"alarms": []}})
    await hass.async_block_till_done()
    assert device._last_status_observed == previous
    await device.recovery._async_update_data()
    pull.assert_called_once_with(device.device_id)
    assert device.remaining_time == 11


@pytest.mark.parametrize("meaning", ["cyclePhase", "remainingTimeMM"])
async def test_meaningful_push_refreshes_status_freshness(hass, machines, meaning):
    device = machines[2][0]
    await running(hass, device)
    device._last_status_observed = time.monotonic() - 100
    before = time.monotonic()
    device.on_message({"event": "status", "payload": {
        "statuses": [{"properties": {device.codes[meaning]: "3"}}]}})
    await hass.async_block_till_done()
    assert device._last_status_observed >= before


async def test_newer_push_wins_over_older_rest_snapshot(hass, machines):
    device = machines[2][0]
    await running(hass, device)
    stale = snapshot(device, status="FINISHED", phase="0", minutes=0)
    read_at = time.monotonic() - 1
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {
        device.codes["cyclePhase"]: "3", device.codes["remainingTimeMM"]: "9"}}]}})
    device.apply_fresh(stale, read_at=read_at)
    await hass.async_block_till_done()
    assert device.state == "running"
    assert device.phase_value == "3"
    assert device.remaining_time == 9
    assert device.program_status == "IN_PROGRESS"


@pytest.mark.parametrize("retry_header", ["120", "date"])
async def test_429_has_no_immediate_retry_and_blocks_both_machines(hass, machines, monkeypatch, retry_header):
    _, cloud, devices = machines
    for device in devices:
        device._last_status_observed = time.monotonic() - 100
    response = Response()
    response.status_code = 429
    response.url = "https://example.invalid/status"
    response.headers["Retry-After"] = (format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
                                       if retry_header == "date" else retry_header)
    get = Mock(return_value=response)
    monkeypatch.setattr(cloud._session, "get", get)
    with pytest.raises(UpdateFailed):
        await devices[0].recovery._async_update_data()
    assert get.call_count == 1, "a 429 must not be retried before Retry-After"
    assert cloud._laundry_retry_until > time.monotonic() + 100
    with pytest.raises(UpdateFailed):
        await devices[1].recovery._async_update_data()
    assert get.call_count == 1, "the second machine shares the same account backoff"
    devices[0].recovery.push_updated()
    await hass.async_block_till_done()
    assert cloud._laundry_retry_until > time.monotonic() + 100
    with pytest.raises(HTTPError):
        await hass.async_add_executor_job(cloud.pull_device_data, devices[1].device_id)
    assert get.call_count == 1, "the general refresh path must respect the same pause"


async def test_unload_removes_background_polling_and_pending_finish(hass, machines, monkeypatch):
    entry, cloud, devices = machines
    device = devices[0]
    await running(hass, device)
    device.update_from_backend(snapshot(device, status="FINISHED", phase="0", minutes=0))
    await hass.async_block_till_done()
    coordinators = [d.recovery for d in devices]
    pull = Mock(return_value=snapshot(device))
    monkeypatch.setattr(cloud, "pull_device_data", pull)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert all(c._closed for c in coordinators)
    assert all(d.recovery is None and d.events._closed for d in devices)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=180))
    await hass.async_block_till_done()
    assert not pull.called


async def test_cached_active_state_cannot_fabricate_completion(hass, machines):
    _, cloud, devices = machines
    device = devices[0]
    await running(hass, device)
    cached = device.cache_dict()
    events = []
    unsubscribe = hass.bus.async_listen(EVENT_TYPE, lambda event: events.append(event.data))
    try:
        cloud.load_cached([cached])
        restored = cloud.devices[-1]
        restored.machine_entity_id = "haier_evo.cached_washer"
        await hass.async_block_till_done()
        assert not restored.synced
        restored.apply_fresh(snapshot(restored, status="FINISHED", phase="0", minutes=0))
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert events == []
        assert not restored.laundry_ready
    finally:
        unsubscribe()
