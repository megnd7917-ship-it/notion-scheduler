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

# Ahead-of-schedule work begins appearing in this part of the queue.
AHEAD_OF_SCHEDULE_START = 18

# Ahead-of-schedule tasks receive a modest allocation.
AHEAD_WORK_MINUTES = 45

# Normal work gets a somewhat larger allocation when appropriate.
NORMAL_WORK_MINUTES = 60

# If doing the work by the day before the deadline would require
# more than this many minutes per day, the actual deadline day is
# included in the calculation.
MAX_PREFERRED_DAILY_MINUTES = 120

# State is used only for queue continuity/bump behavior.
# It is NOT used to track completed work.
STATE_FILE = os.environ.get(
    "SCHEDULER_STATE_FILE",
    ".scheduler_state.json",
)

CREATE_REQUEST_DELAY = 0.5
MAX_RATE_LIMIT_RETRIES = 5

DEPENDENCY_PROPERTY_NAME = "Blocked by"
COMPLETED_UNITS_PROPERTY_NAME = "Completed Units"

SOURCE_TASK_TITLE_PROPERTY = "Task"

TASK_ALLOCATION_DATABASE = "Task Allocations"


# ============================================================
# SOURCE DATABASES
# ============================================================

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


# ============================================================
# WORKLOAD CONVERSIONS
# ============================================================

MINUTES_PER_UNIT = {
    "Pages": 5,
    "LSAT Questions": 3,
    "Questions": 3,
    "Papers": 10,
    "Hours": 60,
    "Minutes": 1,
}


def workload_to_minutes(workload, unit):
    if workload is None or not unit:
        return 0

    if unit not in MINUTES_PER_UNIT:
        raise RuntimeError(
            f'Unknown workload unit "{unit}".'
        )

    return workload * MINUTES_PER_UNIT[unit]


def minutes_to_units(minutes, unit):
    if not unit:
        return 0

    if unit not in MINUTES_PER_UNIT:
        raise RuntimeError(
            f'Unknown workload unit "{unit}".'
        )

    return minutes / MINUTES_PER_UNIT[unit]


# ============================================================
# NOTION API
# ============================================================

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def notion(method, path, **kwargs):
    url = f"https://api.notion.com/v1/{path}"

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):

        response = requests.request(
            method,
            url,
            headers=HEADERS,
            **kwargs,
        )

        if response.ok:
            if not response.content:
                return {}

            return response.json()

        if response.status_code == 429:

            retry_after = 3

            try:
                data = response.json()

                retry_after = int(
                    data.get("error", {})
                    .get("additional_data", {})
                    .get("retry_after", 3)
                )

            except Exception:
                pass

            if attempt >= MAX_RATE_LIMIT_RETRIES:
                raise RuntimeError(
                    "Notion rate limit persisted after "
                    f"{MAX_RATE_LIMIT_RETRIES} retries: "
                    f"{response.text}"
                )

            print(
                "Notion rate limit reached. "
                f"Waiting {retry_after} seconds..."
            )

            time.sleep(max(1, retry_after))
            continue

        raise RuntimeError(
            f"Notion API error "
            f"{response.status_code}: {response.text}"
        )

    raise RuntimeError(
        "Notion API request failed."
    )


def search_all(query):
    results = []
    cursor = None

    while True:

        payload = {
            "query": query,
            "page_size": 100,
        }

        if cursor:
            payload["start_cursor"] = cursor

        data = notion(
            "POST",
            "search",
            json=payload,
        )

        results.extend(
            data.get("results", [])
        )

        if not data.get("has_more"):
            return results

        cursor = data.get("next_cursor")


def find_database(name):
    for obj in search_all(name):

        if obj.get("object") == "database":

            title = "".join(
                item.get("plain_text", "")
                for item in obj.get("title", [])
            ).strip()

            if title == name:
                return obj["id"]

        elif obj.get("object") == "data_source":

            database_id = (
                obj.get("parent", {})
                .get("database_id")
            )

            if not database_id:
                continue

            database = notion(
                "GET",
                f"databases/{database_id}",
            )

            title = "".join(
                item.get("plain_text", "")
                for item in database.get("title", [])
            ).strip()

            if title == name:
                return database_id

    raise RuntimeError(
        f'Could not find Notion database "{name}".'
    )


def get_data_source(database_id):
    data = notion(
        "GET",
        f"databases/{database_id}",
    )

    sources = data.get("data_sources", [])

    if not sources:
        raise RuntimeError(
            f"No data source found for database {database_id}."
        )

    return sources[0]["id"]


def get_data_source_properties(database_id):
    data_source_id = get_data_source(database_id)

    data = notion(
        "GET",
        f"data_sources/{data_source_id}",
    )

    return (
        data_source_id,
        data.get("properties", {})
    )


def ensure_database_property(
    database_id,
    property_name,
    property_type,
    config,
):
    data_source_id, properties = (
        get_data_source_properties(database_id)
    )

    existing = properties.get(property_name)

    if existing:
        if existing.get("type") != property_type:
            raise RuntimeError(
                f'The "{property_name}" property exists but '
                f'is not a {property_type} property.'
            )

        return

    print(
        f'Creating "{property_name}" {property_type} property.'
    )

    notion(
        "PATCH",
        f"data_sources/{data_source_id}",
        json={
            "properties": {
                property_name: config
            }
        },
    )

    time.sleep(CREATE_REQUEST_DELAY)


def ensure_number_property(database_id, property_name):
    ensure_database_property(
        database_id,
        property_name,
        "number",
        {
            "number": {
                "format": "number"
            }
        },
    )


def ensure_checkbox_property(database_id, property_name):
    ensure_database_property(
        database_id,
        property_name,
        "checkbox",
        {
            "checkbox": {}
        },
    )


def ensure_relation_properties(database_id):
    """
    Make sure Task Allocations has one relation for each
    source task database.
    """

    allocation_data_source_id, properties = (
        get_data_source_properties(database_id)
    )

    for source_database_name, relation_name in (
        SOURCE_DATABASES.items()
    ):

        source_database_id = find_database(
            source_database_name
        )

        source_data_source_id = get_data_source(
            source_database_id
        )

        existing = properties.get(relation_name)

        if existing:

            if existing.get("type") != "relation":
                raise RuntimeError(
                    f'The "{relation_name}" property on '
                    "Task Allocations is not a relation."
                )

            relation = existing.get(
                "relation",
                {}
            )

            existing_data_source_id = relation.get(
                "data_source_id"
            )

            existing_database_id = relation.get(
                "database_id"
            )

            if (
                existing_data_source_id
                and existing_data_source_id
                != source_data_source_id
            ):
                raise RuntimeError(
                    f'The "{relation_name}" relation points '
                    "to the wrong data source."
                )

            if (
                not existing_data_source_id
                and existing_database_id
                and existing_database_id
                != source_database_id
            ):
                raise RuntimeError(
                    f'The "{relation_name}" relation points '
                    "to the wrong database."
                )

            continue

        print(
            f'Creating "{relation_name}" relation '
            f'to {source_database_name}.'
        )

        notion(
            "PATCH",
            f"data_sources/{allocation_data_source_id}",
            json={
                "properties": {
                    relation_name: {
                        "relation": {
                            "data_source_id":
                                source_data_source_id,
                            "single_property": {},
                        }
                    }
                }
            },
        )

        time.sleep(CREATE_REQUEST_DELAY)


def query_data_source(data_source_id):
    pages = []
    cursor = None

    while True:

        payload = {
            "page_size": 100
        }

        if cursor:
            payload["start_cursor"] = cursor

        data = notion(
            "POST",
            f"data_sources/{data_source_id}/query",
            json=payload,
        )

        pages.extend(
            data.get("results", [])
        )

        if not data.get("has_more"):
            return pages

        cursor = data.get("next_cursor")


# ============================================================
# PROPERTY HELPERS
# ============================================================

def normalize_notion_id(value):
    if not value:
        return None

    return (
        str(value)
        .replace("-", "")
        .strip()
        .lower()
    )


def title_value(page, property_name):
    prop = (
        page.get("properties", {})
        .get(property_name)
    )

    if not prop or prop.get("type") != "title":
        return ""

    return "".join(
        item.get("plain_text", "")
        for item in prop.get("title", [])
    )


def checkbox_value(page, property_name):
    prop = (
        page.get("properties", {})
        .get(property_name)
    )

    if not prop or prop.get("type") != "checkbox":
        return False

    return bool(
        prop.get("checkbox", False)
    )


def number_value(page, property_name):
    prop = (
        page.get("properties", {})
        .get(property_name)
    )

    if not prop or prop.get("type") != "number":
        return None

    return prop.get("number")


def select_value(page, property_name):
    prop = (
        page.get("properties", {})
        .get(property_name)
    )

    if not prop or prop.get("type") != "select":
        return None

    value = prop.get("select")

    if not value:
        return None

    return value.get("name")


def relation_ids(page, property_name):
    prop = (
        page.get("properties", {})
        .get(property_name)
    )

    if not prop or prop.get("type") != "relation":
        return []

    return [
        item["id"]
        for item in prop.get("relation", [])
    ]


def parse_datetime(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ)


def date_value(page, property_name):
    prop = (
        page.get("properties", {})
        .get(property_name)
    )

    if not prop or prop.get("type") != "date":
        return None

    value = prop.get("date")

    if not value:
        return None

    start = value.get("start")
    end = value.get("end")

    if not start:
        return None

    return {
        "start": parse_datetime(start),
        "end": (
            parse_datetime(end)
            if end
            else None
        ),
    }


# ============================================================
# STATE
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "orders": {},
            "signatures": {},
        }

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError

        data.setdefault("orders", {})
        data.setdefault("signatures", {})

        return data

    except Exception:
        print(
            "Warning: scheduler state could not be read. "
            "Starting with empty queue state."
        )

        return {
            "orders": {},
            "signatures": {},
        }


def save_state(state):
    directory = os.path.dirname(
        os.path.abspath(STATE_FILE)
    )

    os.makedirs(
        directory,
        exist_ok=True,
    )

    temporary = (
        f"{STATE_FILE}.tmp"
    )

    with open(
        temporary,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        temporary,
        STATE_FILE,
    )


# ============================================================
# SOURCE TASKS
# ============================================================

def read_tasks():
    tasks = []

    for database_name, relation_name in (
        SOURCE_DATABASES.items()
    ):

        database_id = find_database(
            database_name
        )

        ensure_number_property(
            database_id,
            COMPLETED_UNITS_PROPERTY_NAME,
        )

        ensure_checkbox_property(
            database_id,
            "Bump",
        )

        ensure_checkbox_property(
            database_id,
            "Cancelled",
        )

        data_source_id = get_data_source(
            database_id
        )

        pages = query_data_source(
            data_source_id
        )

        for page in pages:

            name = title_value(
                page,
                SOURCE_TASK_TITLE_PROPERTY,
            ).strip()

            if not name:
                continue

            workload = number_value(
                page,
                "Workload",
            )

            completed_units = (
                number_value(
                    page,
                    COMPLETED_UNITS_PROPERTY_NAME,
                )
                or 0
            )

            if workload is not None:
                completed_units = max(
                    0,
                    min(
                        completed_units,
                        workload,
                    ),
                )

            unit = select_value(
                page,
                "Unit",
            )

            tasks.append({
                "page_id": page["id"],
                "task": name,
                "database": database_name,
                "relation_name": relation_name,

                "deadline": date_value(
                    page,
                    "Deadline",
                ),

                "workload": workload,
                "completed_units": completed_units,
                "unit": unit,

                "priority": select_value(
                    page,
                    "Priority",
                ),

                "completed": checkbox_value(
                    page,
                    "Completed",
                ),

                "bump": checkbox_value(
                    page,
                    "Bump",
                ),

                "cancelled": checkbox_value(
                    page,
                    "Cancelled",
                ),

                "dependencies": relation_ids(
                    page,
                    DEPENDENCY_PROPERTY_NAME,
                ),

                "minutes": workload_to_minutes(
                    workload,
                    unit,
                ),
            })

    print(
        f"Source tasks found: {len(tasks)}"
    )

    return tasks


def task_completed_minutes(task):
    workload = task.get("workload")
    completed_units = (
        task.get("completed_units", 0)
        or 0
    )

    total_minutes = task.get(
        "minutes",
        0,
    )

    if (
        workload is None
        or workload <= 0
        or total_minutes <= 0
    ):
        return 0

    completed_units = max(
        0,
        min(
            completed_units,
            workload,
        ),
    )

    return (
        total_minutes
        * completed_units
        / workload
    )


def task_remaining_minutes(task):
    return max(
        0,
        task.get("minutes", 0)
        - task_completed_minutes(task),
    )


def task_remaining_units(task):
    workload = task.get("workload")

    if workload is None:
        return 0

    return max(
        0,
        workload
        - (
            task.get(
                "completed_units",
                0,
            )
            or 0
        ),
    )


# ============================================================
# DEADLINE CALCULATIONS
# ============================================================

def available_workdays(
    start_date,
    target_date,
):
    if target_date < start_date:
        return 1

    return max(
        1,
        (
            target_date - start_date
        ).days
        + 1,
    )


def deadline_target_date(
    task,
    now,
):
    deadline = task.get(
        "deadline"
    )

    if not deadline:
        return None

    deadline_day = (
        deadline["start"].date()
    )

    today = now.date()

    if deadline_day <= today:
        return today

    preferred_target = (
        deadline_day
        - timedelta(days=1)
    )

    preferred_days = available_workdays(
        today,
        preferred_target,
    )

    remaining_minutes = (
        task_remaining_minutes(task)
    )

    preferred_daily_minutes = (
        remaining_minutes
        / preferred_days
    )

    if (
        preferred_daily_minutes
        > MAX_PREFERRED_DAILY_MINUTES
    ):
        return deadline_day

    return preferred_target


def required_daily_minutes(
    task,
    now,
):
    target = deadline_target_date(
        task,
        now,
    )

    if target is None:
        return 0

    days = available_workdays(
        now.date(),
        target,
    )

    return (
        task_remaining_minutes(task)
        / days
    )


# ============================================================
# DEPENDENCIES / PRIORITY / SORTING
# ============================================================

def dependency_status(
    task,
    tasks_by_id,
):
    unmet = []

    for dependency_id in task.get(
        "dependencies",
        [],
    ):

        dependency = tasks_by_id.get(
            normalize_notion_id(
                dependency_id
            )
        )

        if (
            dependency
            and not dependency.get(
                "completed",
                False,
            )
            and not dependency.get(
                "cancelled",
                False,
            )
        ):
            unmet.append(
                dependency["task"]
            )

    return (
        bool(unmet),
        unmet,
    )


def priority_weight(priority):
    return {
        "High": 0,
        "Medium": 1,
        "Low": 2,
    }.get(
        priority,
        1,
    )


def task_sort_key(
    task,
    now,
    tasks_by_id,
):
    deadline = task.get(
        "deadline"
    )

    days_left = float("inf")
    overdue = False
    due_today = False

    if deadline:

        deadline_day = (
            deadline["start"].date()
        )

        days_left = (
            deadline_day
            - now.date()
        ).days

        overdue = days_left < 0
        due_today = days_left == 0

    blocked, _ = dependency_status(
        task,
        tasks_by_id,
    )

    daily_minutes = (
        required_daily_minutes(
            task,
            now,
        )
    )

    if overdue:
        urgency_bucket = 0

    elif due_today:
        urgency_bucket = 1

    elif task.get("priority") == "High":
        urgency_bucket = 2

    elif deadline and daily_minutes > 120:
        urgency_bucket = 2

    elif deadline and daily_minutes > 60:
        urgency_bucket = 3

    elif deadline:
        urgency_bucket = 4

    else:
        urgency_bucket = 6

    if blocked:
        urgency_bucket += 5

    return (
        urgency_bucket,
        priority_weight(
            task.get("priority")
        ),
        days_left,
        -daily_minutes,
        task["task"].lower(),
    )


def task_signature(
    task,
    now,
):
    deadline = task.get(
        "deadline"
    )

    deadline_text = None

    if deadline:
        deadline_text = (
            deadline["start"].isoformat()
        )

    return (
        task["task"],
        task.get("workload"),
        task.get("completed_units"),
        task.get("unit"),
        task.get("priority"),
        deadline_text,
        task.get("completed"),
        task.get("cancelled"),
        now.date().isoformat(),
    )


# ============================================================
# TASK ALLOCATION DATABASE
# ============================================================

def allocation_title_property(
    database_id,
):
    _, properties = (
        get_data_source_properties(
            database_id
        )
    )

    for name, prop in properties.items():

        if prop.get("type") == "title":
            return name

    raise RuntimeError(
        "Task Allocations has no title property."
    )


def ensure_allocation_properties(
    database_id,
):
    ensure_number_property(
        database_id,
        "Allocation",
    )

    ensure_number_property(
        database_id,
        "Completed Units",
    )

    ensure_number_property(
        database_id,
        "Schedule Order",
    )

    ensure_checkbox_property(
        database_id,
        "Completed",
    )

    ensure_checkbox_property(
        database_id,
        "Bump",
    )

    ensure_checkbox_property(
        database_id,
        "Cancel",
    )

    ensure_relation_properties(
        database_id
    )


def read_allocations():
    database_id = find_database(
        TASK_ALLOCATION_DATABASE
    )

    data_source_id = get_data_source(
        database_id
    )

    pages = query_data_source(
        data_source_id
    )

    title_name = allocation_title_property(
        database_id
    )

    allocations = []

    for page in pages:

        source_links = {}

        for relation_name in (
            SOURCE_DATABASES.values()
        ):

            ids = relation_ids(
                page,
                relation_name,
            )

            if ids:
                source_links[
                    relation_name
                ] = ids

        allocations.append({
            "page_id": page["id"],

            "name": title_value(
                page,
                title_name,
            ),

            "source_links": source_links,

            "allocation": (
                number_value(
                    page,
                    "Allocation",
                )
                or 0
            ),

            "completed_units": (
                number_value(
                    page,
                    "Completed Units",
                )
                or 0
            ),

            "unit": select_value(
                page,
                "Unit",
            ),

            "completed": checkbox_value(
                page,
                "Completed",
            ),

            "bump": checkbox_value(
                page,
                "Bump",
            ),

            "cancel": checkbox_value(
                page,
                "Cancel",
            ),

            "schedule_order": (
                number_value(
                    page,
                    "Schedule Order",
                )
            ),
        })

    print(
        f"Task Allocations found: "
        f"{len(allocations)}"
    )

    return allocations


def allocation_task_id(
    allocation,
    tasks_by_id,
):
    normalized = {
        normalize_notion_id(
            task_id
        ): task_id
        for task_id in tasks_by_id
    }

    matches = []

    for ids in allocation[
        "source_links"
    ].values():

        for raw_id in ids:

            task_id = normalized.get(
                normalize_notion_id(
                    raw_id
                )
            )

            if (
                task_id
                and task_id not in matches
            ):
                matches.append(
                    task_id
                )

    if not matches:
        return None

    if len(matches) > 1:
        print(
            f'Warning: allocation '
            f'"{allocation["name"]}" is linked '
            "to multiple tasks."
        )

    return matches[0]


# ============================================================
# ALLOCATION AMOUNTS
# ============================================================

def allocation_units_for_task(
    task,
    now,
    rank,
):
    remaining_units = (
        task_remaining_units(task)
    )

    if remaining_units <= 0:
        return 0

    daily_minutes = (
        required_daily_minutes(
            task,
            now,
        )
    )

    if daily_minutes > 0:

        if rank >= AHEAD_OF_SCHEDULE_START:
            minutes = min(
                daily_minutes,
                AHEAD_WORK_MINUTES,
            )
        else:
            minutes = max(
                daily_minutes,
                NORMAL_WORK_MINUTES
                if daily_minutes > NORMAL_WORK_MINUTES
                else daily_minutes,
            )

        units = minutes_to_units(
            minutes,
            task["unit"],
        )

        return max(
            1,
            min(
                remaining_units,
                units,
            ),
        )

    # No deadline:
    # deliberately keep these allocations modest.
    minutes = (
        AHEAD_WORK_MINUTES
        if rank >= AHEAD_OF_SCHEDULE_START
        else NORMAL_WORK_MINUTES
    )

    units = minutes_to_units(
        minutes,
        task["unit"],
    )

    return max(
        1,
        min(
            remaining_units,
            units,
        ),
    )


# ============================================================
# NOTION ALLOCATION UPDATES
# ============================================================

def update_allocation(
    page_id,
    properties,
):
    if not properties:
        return

    notion(
        "PATCH",
        f"pages/{page_id}",
        json={
            "properties": properties
        },
    )


def create_allocation(
    database_id,
    task,
    units,
    order,
):
    title_name = (
        allocation_title_property(
            database_id
        )
    )

    properties = {
        title_name: {
            "title": [
                {
                    "text": {
                        "content":
                            task["task"]
                    }
                }
            ]
        },

        "Allocation": {
            "number": units
        },

        "Completed Units": {
            "number": 0
        },

        "Completed": {
            "checkbox": False
        },

        "Bump": {
            "checkbox": False
        },

        "Cancel": {
            "checkbox": False
        },

        "Schedule Order": {
            "number": order
        },

        "Unit": {
            "select": {
                "name": task["unit"]
            }
        },

        task["relation_name"]: {
            "relation": [
                {
                    "id":
                        task["page_id"]
                }
            ]
        },
    }

    notion(
        "POST",
        "pages",
        json={
            "parent": {
                "database_id":
                    database_id
            },
            "properties":
                properties,
        },
    )

    time.sleep(
        CREATE_REQUEST_DELAY
    )


def archive_page(page_id):
    notion(
        "PATCH",
        f"pages/{page_id}",
        json={
            "in_trash": True
        },
    )


# ============================================================
# MASTER TASK UPDATES
# ============================================================

def update_master(
    task,
    properties,
):
    if not properties:
        return

    notion(
        "PATCH",
        f"pages/{task['page_id']}",
        json={
            "properties": properties
        },
    )


def process_completed_units(
    allocations,
    tasks_by_id,
):
    """
    Completed Units on an allocation means:

        "How many units did I actually complete
         from this allocation?"

    The amount is immediately credited to the master task,
    then the allocation's Completed Units is reset to zero.

    This means a scheduler rerun can never credit the same
    seven pages twice.
    """

    changed = False

    for allocation in allocations:

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            continue

        task = tasks_by_id[
            task_id
        ]

        completed_units = (
            allocation.get(
                "completed_units",
                0,
            )
            or 0
        )

        if completed_units <= 0:
            continue

        workload = task.get(
            "workload"
        )

        if workload is None:
            print(
                f'Warning: cannot credit '
                f'{allocation["name"]} because '
                "the master task has no workload."
            )

            update_allocation(
                allocation["page_id"],
                {
                    "Completed Units": {
                        "number": 0
                    }
                },
            )

            continue

        current_total = (
            task.get(
                "completed_units",
                0,
            )
            or 0
        )

        new_total = min(
            workload,
            current_total
            + completed_units,
        )

        if new_total != current_total:

            properties = {
                "Completed Units": {
                    "number": new_total
                }
            }

            if new_total >= workload:
                properties[
                    "Completed"
                ] = {
                    "checkbox": True
                }

            update_master(
                task,
                properties,
            )

            task[
                "completed_units"
            ] = new_total

            if new_total >= workload:
                task[
                    "completed"
                ] = True

            changed = True

        # Clear the allocation's one-time
        # progress entry after crediting it.
        update_allocation(
            allocation["page_id"],
            {
                "Completed Units": {
                    "number": 0
                }
            },
        )

        allocation[
            "completed_units"
        ] = 0

        print(
            f'Credited {completed_units:g} '
            f'{task["unit"]} to '
            f'"{task["task"]}".'
        )

    return changed


def process_completed_allocations(
    allocations,
    tasks_by_id,
):
    """
    Checking Completed means the user completed
    the entire current allocation.
    """

    changed = False

    for allocation in allocations:

        if not allocation.get(
            "completed"
        ):
            continue

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            continue

        task = tasks_by_id[
            task_id
        ]

        workload = task.get(
            "workload"
        )

        if workload is None:
            update_allocation(
                allocation["page_id"],
                {
                    "Completed": {
                        "checkbox": False
                    }
                },
            )

            continue

        current_total = (
            task.get(
                "completed_units",
                0,
            )
            or 0
        )

        new_total = workload

        if current_total < new_total:

            update_master(
                task,
                {
                    "Completed Units": {
                        "number":
                            new_total
                    },
                    "Completed": {
                        "checkbox":
                            True
                    },
                },
            )

            task[
                "completed_units"
            ] = new_total

            task[
                "completed"
            ] = True

        update_allocation(
            allocation["page_id"],
            {
                "Completed": {
                    "checkbox": False
                },
                "Completed Units": {
                    "number": 0
                },
            },
        )

        allocation[
            "completed"
        ] = False

        changed = True

        print(
            f'Completed task: '
            f'"{task["task"]}".'
        )

    return changed


def process_cancellations(
    allocations,
    tasks_by_id,
):
    """
    Cancel is intentionally different from Bump.

    Cancel means the task no longer needs to appear
    in the scheduler. The master task receives
    Cancelled = True, and the allocation is archived.
    """

    changed = False
    cancelled_page_ids = set()

    for allocation in allocations:

        if not allocation.get(
            "cancel"
        ):
            continue

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            # Still remove the orphaned allocation.
            archive_page(
                allocation["page_id"]
            )
            changed = True
            continue

        task = tasks_by_id[
            task_id
        ]

        update_master(
            task,
            {
                "Cancelled": {
                    "checkbox": True
                }
            },
        )

        task[
            "cancelled"
        ] = True

        archive_page(
            allocation["page_id"]
        )

        cancelled_page_ids.add(
            allocation["page_id"]
        )

        changed = True

        print(
            f'Cancelled task: '
            f'"{task["task"]}".'
        )

    return (
        changed,
        cancelled_page_ids,
    )


def sync_master_completion(
    tasks,
):
    changed = False

    for task in tasks:

        workload = task.get(
            "workload"
        )

        if workload is None:
            continue

        completed_units = (
            task.get(
                "completed_units",
                0,
            )
            or 0
        )

        if (
            completed_units >= workload
            and not task.get(
                "completed"
            )
        ):

            update_master(
                task,
                {
                    "Completed": {
                        "checkbox": True
                    }
                },
            )

            task[
                "completed"
            ] = True

            changed = True

    return changed


# ============================================================
# BUMP
# ============================================================

def apply_bumps(
    ordered_tasks,
    allocations_by_task,
):
    """
    Bump is an action, not a permanent task state.

    The current queue is taken as the starting order and
    the bumped task is moved down exactly three positions.

    The resulting order is written into Schedule Order,
    so it persists until a meaningful queue recalculation.
    """

    result = list(
        ordered_tasks
    )

    bumped = []

    for task in list(result):

        allocation = (
            allocations_by_task.get(
                normalize_notion_id(
                    task["page_id"]
                )
            )
        )

        if not allocation:
            continue

        if not allocation.get(
            "bump"
        ):
            continue

        bumped.append(
            allocation
        )

    for allocation in bumped:

        task_id = allocation_task_id(
            allocation,
            {
                normalize_notion_id(
                    task["page_id"]
                ): task
                for task in result
            },
        )

        if not task_id:
            continue

        current_index = None

        for index, task in enumerate(
            result
        ):
            if normalize_notion_id(
                task["page_id"]
            ) == normalize_notion_id(
                task_id
            ):
                current_index = index
                break

        if current_index is None:
            continue

        new_index = min(
            len(result) - 1,
            current_index + 3,
        )

        task = result.pop(
            current_index
        )

        result.insert(
            new_index,
            task,
        )

        update_allocation(
            allocation["page_id"],
            {
                "Bump": {
                    "checkbox": False
                }
            },
        )

        allocation[
            "bump"
        ] = False

        print(
            f'Bumped "{task["task"]}" '
            f"from position "
            f"{current_index + 1} to "
            f"{new_index + 1}."
        )

    return result


# ============================================================
# QUEUE RECONCILIATION
# ============================================================

def build_desired_order(
    tasks,
    tasks_by_id,
    now,
):
    return sorted(
        tasks,
        key=lambda task:
            task_sort_key(
                task,
                now,
                tasks_by_id,
            ),
    )


def allocation_map(
    allocations,
    tasks_by_id,
):
    result = {}

    for allocation in allocations:

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            continue

        normalized = (
            normalize_notion_id(
                task_id
            )
        )

        if normalized in result:
            # Duplicate allocations for the same task
            # are handled later.
            result[normalized].append(
                allocation
            )
        else:
            result[normalized] = [
                allocation
            ]

    return result


def choose_existing_allocation(
    allocations,
):
    """
    If duplicates exist, preserve the allocation with
    the lowest existing Schedule Order.

    The others are archived as true duplicates.
    """

    if not allocations:
        return None, []

    sorted_allocations = sorted(
        allocations,
        key=lambda allocation: (
            allocation.get(
                "schedule_order"
            )
            if allocation.get(
                "schedule_order"
            ) is not None
            else 999999,
            allocation["page_id"],
        ),
    )

    return (
        sorted_allocations[0],
        sorted_allocations[1:],
    )


def reconcile_queue(
    tasks,
    allocations,
    now,
    state,
):
    tasks_by_id = {
        normalize_notion_id(
            task["page_id"]
        ): task
        for task in tasks
    }

    # --------------------------------------------------------
    # Remove completed/cancelled allocations.
    # --------------------------------------------------------

    for allocation in allocations:

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            # Orphaned allocation.
            print(
                f'Archiving orphaned allocation '
                f'"{allocation["name"]}".'
            )

            archive_page(
                allocation["page_id"]
            )

            continue

        task = tasks_by_id[
            task_id
        ]

        if (
            task.get("completed")
            or task.get("cancelled")
            or task_remaining_minutes(
                task
            ) <= 0
        ):
            archive_page(
                allocation["page_id"]
            )

    # Re-read because some allocations may have
    # been archived above.
    allocations = read_allocations()

    allocation_groups = allocation_map(
        allocations,
        tasks_by_id,
    )

    active_tasks = [
        task
        for task in tasks
        if not task.get("completed")
        and not task.get("cancelled")
        and task_remaining_minutes(
            task
        ) > 0
    ]

    active_ids = {
        normalize_notion_id(
            task["page_id"]
        )
        for task in active_tasks
    }

    # --------------------------------------------------------
    # Remove duplicate allocations, but ONLY duplicates.
    # --------------------------------------------------------

    primary_allocations = {}

    for task_id, group in (
        allocation_groups.items()
    ):

        primary, duplicates = (
            choose_existing_allocation(
                group
            )
        )

        if primary:
            primary_allocations[
                task_id
            ] = primary

        for duplicate in duplicates:

            print(
                f'Archiving duplicate allocation '
                f'"{duplicate["name"]}".'
            )

            archive_page(
                duplicate["page_id"]
            )

    # --------------------------------------------------------
    # Determine whether the queue needs a fresh ordering.
    #
    # We do NOT automatically reorder everything just because
    # the scheduler ran.
    # --------------------------------------------------------

    signatures = state.setdefault(
        "signatures",
        {}
    )

    current_signatures = {
        normalize_notion_id(
            task["page_id"]
        ): task_signature(
            task,
            now,
        )
        for task in active_tasks
    }

    queue_needs_reorder = (
        current_signatures
        != signatures
    )

    if queue_needs_reorder:

        ordered = build_desired_order(
            active_tasks,
            tasks_by_id,
            now,
        )

        print(
            "Queue inputs changed; "
            "recalculating order."
        )

    else:

        # Nothing relevant changed.
        # Preserve the current Task Allocation order.
        order_pairs = []

        for task in active_tasks:

            task_id = normalize_notion_id(
                task["page_id"]
            )

            allocation = (
                primary_allocations.get(
                    task_id
                )
            )

            existing_order = None

            if allocation:
                existing_order = (
                    allocation.get(
                        "schedule_order"
                    )
                )

            if existing_order is None:
                existing_order = 999999

            order_pairs.append(
                (
                    existing_order,
                    task,
                )
            )

        ordered = [
            task
            for _, task in sorted(
                order_pairs,
                key=lambda pair: (
                    pair[0],
                    pair[1]["task"].lower(),
                ),
            )
        ]

        print(
            "Queue inputs unchanged; "
            "preserving existing order."
        )

    # --------------------------------------------------------
    # Apply Bump after establishing the starting order.
    # --------------------------------------------------------

    allocations_for_bump = {}

    for task_id, allocation in (
        primary_allocations.items()
    ):
        allocations_for_bump[
            task_id
        ] = allocation

    ordered = apply_bumps(
        ordered,
        allocations_for_bump,
    )

    # --------------------------------------------------------
    # Reconcile every active task in place.
    # --------------------------------------------------------

    allocation_count = 0
    created_count = 0
    updated_count = 0

    database_id = find_database(
        TASK_ALLOCATION_DATABASE
    )

    for index, task in enumerate(
        ordered,
        start=1,
    ):

        task_id = normalize_notion_id(
            task["page_id"]
        )

        allocation = (
            primary_allocations.get(
                task_id
            )
        )

        units = (
            allocation_units_for_task(
                task,
                now,
                index,
            )
        )

        if units <= 0:
            continue

        if allocation is None:

            create_allocation(
                database_id,
                task,
                units,
                index,
            )

            created_count += 1
            allocation_count += 1

            print(
                f'Created allocation: '
                f'"{task["task"]}" '
                f"= {units:g} "
                f'{task["unit"]}'
            )

            continue

        allocation_count += 1

        properties = {}

        old_units = (
            allocation.get(
                "allocation",
                0,
            )
            or 0
        )

        if abs(
            old_units - units
        ) > 0.000001:

            properties[
                "Allocation"
            ] = {
                "number": units
            }

        old_unit = allocation.get(
            "unit"
        )

        if old_unit != task["unit"]:

            properties[
                "Unit"
            ] = {
                "select": {
                    "name":
                        task["unit"]
                }
            }

        old_order = allocation.get(
            "schedule_order"
        )

        if old_order != index:

            properties[
                "Schedule Order"
            ] = {
                "number": index
            }

        if properties:

            update_allocation(
                allocation["page_id"],
                properties,
            )

            updated_count += 1

            print(
                f'Updated allocation: '
                f'"{task["task"]}"'
            )

    # --------------------------------------------------------
    # Save queue signatures AFTER reconciliation.
    # --------------------------------------------------------

    state["signatures"] = current_signatures

    # Keep order information useful for diagnostics and
    # future recovery.
    state["orders"] = {
        normalize_notion_id(
            task["page_id"]
        ): index
        for index, task in enumerate(
            ordered,
            start=1,
        )
    }

    save_state(
        state
    )

    print(
        "Queue reconciled: "
        f"{allocation_count} existing/active allocations, "
        f"{created_count} created, "
        f"{updated_count} updated."
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("NOTION TASK QUEUE SCHEDULER")
    print("=" * 60)

    state = load_state()

    # --------------------------------------------------------
    # Read source tasks.
    # --------------------------------------------------------

    tasks = read_tasks()

    # --------------------------------------------------------
    # Ensure Task Allocations has the properties it actually
    # needs. No Focus Time or Plan Date properties are created.
    # --------------------------------------------------------

    allocation_database_id = find_database(
        TASK_ALLOCATION_DATABASE
    )

    ensure_allocation_properties(
        allocation_database_id
    )

    # --------------------------------------------------------
    # Read current allocations.
    # --------------------------------------------------------

    allocations = read_allocations()

    tasks_by_id = {
        normalize_notion_id(
            task["page_id"]
        ): task
        for task in tasks
    }

    # --------------------------------------------------------
    # Process user actions BEFORE calculating the new queue.
    # --------------------------------------------------------

    progress_changed = (
        process_completed_units(
            allocations,
            tasks_by_id,
        )
    )

    completion_changed = (
        process_completed_allocations(
            allocations,
            tasks_by_id,
        )
    )

    cancellation_changed, cancelled_ids = (
        process_cancellations(
            allocations,
            tasks_by_id,
        )
    )

    sync_master_completion(
        tasks
    )

    # --------------------------------------------------------
    # Read source tasks again so the scheduler is working from
    # the actual master-task values after user actions.
    # --------------------------------------------------------

    tasks = read_tasks()

    now = datetime.now(TZ)

    allocations = read_allocations()

    # --------------------------------------------------------
    # Reconcile existing allocations rather than deleting and
    # recreating them.
    # --------------------------------------------------------

    reconcile_queue(
        tasks,
        allocations,
        now,
        state,
    )

    print("=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
