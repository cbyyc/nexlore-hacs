"""Constants for the NEXLORE integration."""

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "nexlore"
PLATFORMS = [Platform.TODO]

CONF_BASE_URL = "base_url"
CONF_API_TOKEN = "api_token"
CONF_SPACE = "space"

DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)
STORAGE_VERSION = 1
TASKS_PAGE_SIZE = 500
