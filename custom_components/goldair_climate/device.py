"""
API for Goldair Tuya devices.
"""

from time import time
from threading import Timer, Lock
import logging
import json

from homeassistant.const import TEMP_CELSIUS
from .const import API_PROTOCOL_VERSIONS

_LOGGER = logging.getLogger(__name__)


class GoldairTuyaDevice(object):
    def __init__(self, name, dev_id, address, local_key, hass):
        """
        Represents a Goldair Tuya-based device.

        Args:
            dev_id (str): The device id.
            address (str): The network address.
            local_key (str): The encryption key.
        """
        import pytuya
        self._name = name
        self._api_protocol_version_index = None
        self._api = pytuya.Device(dev_id, address, local_key, 'device')
        self._rotate_api_protocol_version()

        self._fixed_properties = {}
        self._reset_cached_state()

        self._TEMPERATURE_UNIT = TEMP_CELSIUS
        self._hass = hass

        # API calls to update Goldair heaters are asynchronous and non-blocking. This means
        # you can send a change and immediately request an updated state (like HA does),
        # but because it has not yet finished processing you will be returned the old state.
        # The solution is to keep a temporary list of changed properties that we can overlay
        # onto the state while we wait for the board to update its switches.
        self._FAKE_IT_TIL_YOU_MAKE_IT_TIMEOUT = 10
        self._CACHE_TIMEOUT = 20
        self._CONNECTION_ATTEMPTS = 4
        self._lock = Lock()

    @property
    def name(self):
        return self._name

    @property
    def temperature_unit(self):
        return self._TEMPERATURE_UNIT

    def set_fixed_properties(self, fixed_properties):
        self._fixed_properties = fixed_properties
        set_fixed_properties = Timer(10, lambda: self._set_properties(self._fixed_properties))
        set_fixed_properties.start()

    def refresh(self):
        now = time()
        cached_state = self._get_cached_state()
        if now - cached_state['updated_at'] >= self._CACHE_TIMEOUT:
            self._cached_state['updated_at'] = time()
            self._retry_on_failed_connection(
                lambda: self._refresh_cached_state(),
                f'Failed to refresh device state for {self.name}.'
            )

    async def async_refresh(self):
        await self._hass.async_add_executor_job(self.refresh)

    def get_property(self, dps_id):
        cached_state = self._get_cached_state()
        if dps_id in cached_state:
            return cached_state[dps_id]
        else:
            return None

    def set_property(self, dps_id, value):
        self._set_properties({dps_id: value})

    async def async_set_property(self, dps_id, value):
        await self._hass.async_add_executor_job(self.set_property, dps_id, value)

    def anticipate_property_value(self, dps_id, value):
        """
        Update a value in the cached state only. This is good for when you know the device will reflect a new state in
        the next update, but don't want to wait for that update for the device to represent this state.

        The anticipated value will be cleared with the next update.
        """
        self._cached_state[dps_id] = value

    def _reset_cached_state(self):
        self._cached_state = {
            'updated_at': 0
        }
        self._pending_updates = {}

    def _refresh_cached_state(self):
        new_state = self._api.status()
        self._cached_state = new_state['dps']
        self._cached_state['updated_at'] = time()
        _LOGGER.info(f'refreshed device state: {json.dumps(new_state)}')
        _LOGGER.debug(f'new cache state (including pending properties): {json.dumps(self._get_cached_state())}')

    def _set_properties(self, properties):
        if len(properties) == 0:
            return

        self._add_properties_to_pending_updates(properties)
        self._debounce_sending_updates()

    def _add_properties_to_pending_updates(self, properties):
        now = time()
        properties = {**properties, **self._fixed_properties}

        pending_updates = self._get_pending_updates()
        for key, value in properties.items():
            pending_updates[key] = {
                'value': value,
                'updated_at': now
            }

        _LOGGER.debug(f'new pending updates: {json.dumps(self._pending_updates)}')

    def _debounce_sending_updates(self):
        try:
            self._debounce.cancel()
        except AttributeError:
            pass
        self._debounce = Timer(1, self._send_pending_updates)
        self._debounce.start()

    def _send_pending_updates(self):
        pending_properties = self._get_pending_properties()
        payload = self._api.generate_payload('set', pending_properties)

        _LOGGER.info(f'sending dps update: {json.dumps(pending_properties)}')

        self._retry_on_failed_connection(lambda: self._send_payload(payload), 'Failed to update device state.')

    def _send_payload(self, payload):
        try:
            self._lock.acquire()
            self._api._send_receive(payload)
            self._cached_state['updated_at'] = 0
            now = time()
            pending_updates = self._get_pending_updates()
            for key, value in pending_updates.items():
                pending_updates[key]['updated_at'] = now
        finally:
            self._lock.release()

    def _retry_on_failed_connection(self, func, error_message):
        for i in range(self._CONNECTION_ATTEMPTS):
            try:
                func()
            except:
                if i + 1 == self._CONNECTION_ATTEMPTS:
                    self._reset_cached_state()
                    _LOGGER.error(error_message)
                else:
                    self._rotate_api_protocol_version()

    def _get_cached_state(self):
        cached_state = self._cached_state.copy()
        _LOGGER.debug(f'pending updates: {json.dumps(self._get_pending_updates())}')
        return {**cached_state, **self._get_pending_properties()}

    def _get_pending_properties(self):
        return {key: info['value'] for key, info in self._get_pending_updates().items()}

    def _get_pending_updates(self):
        now = time()
        self._pending_updates = {key: value for key, value in self._pending_updates.items()
                                 if now - value['updated_at'] < self._FAKE_IT_TIL_YOU_MAKE_IT_TIMEOUT}
        return self._pending_updates

    def _rotate_api_protocol_version(self):
        if self._api_protocol_version_index is None:
            self._api_protocol_version_index = 0
        else:
            self._api_protocol_version_index += 1

        if self._api_protocol_version_index >= len(API_PROTOCOL_VERSIONS):
            self._api_protocol_version_index = 0

        new_version = API_PROTOCOL_VERSIONS[self._api_protocol_version_index]
        _LOGGER.info(f'Setting protocol version for {self.name} to {new_version}.')
        self._api.set_version(new_version)

    @staticmethod
    def get_key_for_value(obj, value, fallback=None):
        keys = list(obj.keys())
        values = list(obj.values())
        return keys[values.index(value)] or fallback