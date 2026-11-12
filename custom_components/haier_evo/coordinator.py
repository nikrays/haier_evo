"""Read-only laundry recovery when cloud push stops delivering useful state."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from requests.exceptions import HTTPError, RequestException

from .api import AuthUserError, ReauthRequired
from .logger import _LOGGER
from .limits import retry_after_seconds


class HaierLaundryCoordinator(DataUpdateCoordinator[None]):
    def __init__(self, hass, entry, device):
        self.device = device
        self.cloud = device._haier
        self._failures = 0
        self._retry_until = 0.0
        self._closed = False
        if not hasattr(self.cloud, "_laundry_poll_lock"):
            self.cloud._laundry_poll_lock = asyncio.Lock()
            self.cloud._laundry_retry_until = getattr(self.cloud, "_laundry_retry_until", 0.0)
            self.cloud._laundry_next_request = 0.0
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"Haier {device.device_name}",
                         update_interval=self.normal_interval)
        device.recovery = self

    @property
    def normal_interval(self):
        seconds = 60 if not self.device.connected else 30 if self.device.state in ("running", "paused", "care") else 120
        return timedelta(seconds=seconds)

    def request_refresh(self):
        self.hass.loop.call_soon_threadsafe(self._request_refresh)

    @callback
    def _request_refresh(self):
        if not self._closed:
            self.hass.async_create_task(self.async_request_refresh())

    def push_updated(self):
        self.hass.loop.call_soon_threadsafe(self._accept_push)

    @callback
    def _accept_push(self):
        if self._closed or self.cloud._laundry_retry_until > time.monotonic():
            return
        if time.monotonic() - self.device._last_status_observed < 45:
            self._failures = 0
            self._retry_until = 0.0
        interval = self.normal_interval
        if interval != self.update_interval:
            self.update_interval = interval
            if self._listeners:
                self._schedule_refresh()

    async def _async_update_data(self):
        if (self.device.connected and self.device.synced and not self.device._start_pending and self.device.state in ("running", "paused", "care")
                and time.monotonic() - self.device._last_status_observed < 45):
            self.update_interval = self.normal_interval
            return
        async with self.cloud._laundry_poll_lock:
            if self._closed or self.cloud.disconnect_requested:
                return
            retry = max(self._retry_until, self.cloud._laundry_retry_until) - time.monotonic()
            if retry > 0:
                raise UpdateFailed("Haier cloud retry is pending", retry_after=retry)
            delay = self.cloud._laundry_next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._closed:
                return
            started = time.monotonic()
            self.cloud._laundry_next_request = started + 15
            try:
                data = await self.hass.async_add_executor_job(self.cloud.pull_device_data, self.device.device_id)
                if self._closed:
                    return
                if self.device.apply_fresh(data, read_at=started):
                    self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            except (ReauthRequired, AuthUserError):
                self.config_entry.async_start_reauth(self.hass)
                raise
            except (RequestException, ValueError) as err:
                self._failures += 1
                delay = min(60 * 2 ** min(self._failures - 1, 4), 900)
                if isinstance(err, HTTPError) and err.response is not None:
                    delay = retry_after_seconds(err.response.headers.get("Retry-After"), delay)
                    if err.response.status_code in (429, 503):
                        self.cloud._laundry_retry_until = time.monotonic() + delay
                self._retry_until = time.monotonic() + delay
                self.update_interval = timedelta(seconds=delay)
                raise UpdateFailed(f"Unable to refresh laundry: {type(err).__name__}", retry_after=delay) from err
            self._failures = 0
            self._retry_until = 0.0
            self.update_interval = self.normal_interval

    async def async_shutdown(self):
        self._closed = True
        self.device.recovery = None
        await super().async_shutdown()
