import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


# ============================================================
# SETTINGS
# ============================================================

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_VERSION = "2026-03-11"
TZ = ZoneInfo("America/Los_Angeles")

PLANNING_DAYS = 14

MIN_CHUNK_MINUTES = 15
PREFERRED_MAX_CHUNK_MINUTES = 45

# The scheduler keeps ahead-of-schedule work modest so the visible queue
# remains useful rather than becoming an enormous backlog.
AHEAD_OF_SCHEDULE_START = 18
AHEAD_WORK_MINUTES = 45
NORMAL_WORK_MINUTES = 60
MAX_PREFERRED_DAILY_MINUTES = 120

DEPENDENCY_PROPERTY_NAME = "Blocked by"
COMPLETED_UNITS_PROPERTY_NAME = "Completed Units"

PFS_TASK_NAME = "PFS weekly hours"
PFS_DATABASE_NAME = "PFS To-Do"
PFS_WEEKLY_TARGET_MINUTES = 30 * 60

# Only these task databases create a hard "must fit before deadline"
# scheduling deficit. Other deadlines remain useful as soft scheduling
# preferences, but they do not make the schedule report a capacity
# shortfall.
HARD_DEADLINE_DATABASES = {
    "COM 210 To-Do",
    "ENL 248 To-Do",
    "Independent Study To-Do",
    "LSAT To-Do",
    "Applications To-Do",
}

CREATE_REQUEST_DELAY = 0.5
MAX_RATE_LIMIT_RETRIES = 5



# ============================================================
# SOURCE DATABASES
# ============================================================

# Each source database has one corresponding one-page relation
# in Task Allocations.
#
# The scheduler reads tasks directly from these databases.
# There is no Master To-Do List.

SOURCE_DATABASES = {
    "JST To-Do": "JST Task",
    "Reader To-Do": "Reader Task",
    "Applications To-Do": "Applications Task",
    "COM 210 To-Do": "COM 210 Task",
    "ENL 248 To-Do": "ENL 248 Task",
    "Independent Study To-Do": "Independent Study Task",
    "LSAT To-Do": "LSAT Task",
    "PFS To-Do": "PFS Task",
    "Personal To-Do": "Personal Task",
}

# Short labels shown only on Task Allocations / Daily Plan titles.
SOURCE_LABELS = {
    "JST To-Do": "JST",
    "Reader To-Do": "Reader",
    "Applications To-Do": "Applications",
    "COM 210 To-Do": "COM 210",
    "ENL 248 To-Do": "ENL 248",
    "Independent Study To-Do": "Independent Study",
    "LSAT To-Do": "LSAT",
    "PFS To-Do": "PFS",
    "Personal To-Do": "Personal",
}


# ============================================================
# WORKLOAD CONVERSIONS
# ============================================================

MINUTES_PER_UNIT = {
    "Pages": 5,
