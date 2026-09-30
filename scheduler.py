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

# Reserve part of each Focus Time day for work whose deadline is still ahead.
# This prevents urgent work from consuming every available block and creating
# a permanent catch-up cycle. The reserve is used whenever the urgent workload
# can fit without it; genuinely overloaded urgent days may temporarily consume
# the reserve.
AHEAD_CAPACITY_FRACTION = 0.20

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
    if unit not in MINUTES_PER_UNIT:
        raise RuntimeError(
            f'Unknown workload unit "{unit}".'
        )

    return minutes / MINUTES_PER_UNIT[unit]


def format_minutes(minutes):
    minutes = int(round(minutes))

    hours = minutes // 60
    remainder = minutes % 60

    parts = []

    if hours:
        parts.append(
            "1 hour" if hours == 1 else f"{hours} hours"
        )

    if remainder:
        parts.append(
            "1 minute"
            if remainder == 1
            else f"{remainder} minutes"
        )

    return " ".join(parts) if parts else "0 minutes"


def format_allocation(minutes, unit):
    if unit in ("Hours", "Minutes"):
        return format_minutes(minutes)

    amount = minutes_to_units(minutes, unit)

    if amount == int(amount):
        amount_text = str(int(amount))
    else:
        amount_text = f"{amount:g}"

    return f"{amount_text} {unit}"


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
            f"No data source found for database "
            f"{database_id}."
        )

    return sources[0]["id"]


def ensure_database_property(database_id, property_name, property_type, config):
    """Ensure a property exists on the database's current data source.

    Notion's newer API versions expose database properties through data sources.
    Updating only /databases/{id} can leave the property absent from the data
    source that is actually used by page creation/update calls, which then
    produces errors such as "Plan Date is not a property that exists".
    """
    database = notion(
        "GET",
        f"databases/{database_id}",
    )

    properties = database.get("properties", {})
    existing = properties.get(property_name)
    if existing:
        if existing.get("type") != property_type:
            raise RuntimeError(
                f'The "{property_name}" property exists but is not a '
                f'{property_type} property.'
            )
        return

    data_sources = database.get("data_sources", [])
    if not data_sources:
        raise RuntimeError(
            f"No data source found for database {database_id} while creating "
            f'"{property_name}".'
        )

    data_source_id = data_sources[0]["id"]
    print(
        f'Creating "{property_name}" {property_type} property in '
        f'data source {data_source_id}.'
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


def ensure_number_property(database_id, property_name):
    """Create a number property on a database if it does not already exist."""
    ensure_database_property(
        database_id,
        property_name,
        "number",
        {"number": {"format": "number"}},
    )


def ensure_date_property(database_id, property_name):
    """Create a date property on a database if it does not already exist."""
    ensure_database_property(
        database_id,
        property_name,
        "date",
        {"date": {}},
    )


def ensure_allocation_relation_properties(database_id):
    """Ensure Task Allocations has one relation property for every source task database.

    The scheduler writes the source task page into a relation whose property name
    comes from SOURCE_DATABASES. Older versions of the scheduler assumed those
    relation properties already existed. If one is missing (as happened with
    ``COM 210 Task``), Notion rejects the page creation with a 400 validation
    error. Create any missing one-way relations here before allocations are
    read or created.
    """
    allocation_data_source_id = get_data_source(database_id)

    allocation_source = notion(
        "GET",
        f"data_sources/{allocation_data_source_id}",
    )
    existing_properties = allocation_source.get("properties", {})

    for source_database_name, relation_name in SOURCE_DATABASES.items():
        source_database_id = find_database(source_database_name)
        source_data_source_id = get_data_source(source_database_id)

        existing = existing_properties.get(relation_name)
        if existing:
            if existing.get("type") != "relation":
                raise RuntimeError(
                    f'The "{relation_name}" property on Task Allocations exists '
                    f'but is not a relation property.'
                )

            relation_config = existing.get("relation", {})
            target_data_source_id = relation_config.get("data_source_id")
            target_database_id = relation_config.get("database_id")

            if target_data_source_id and target_data_source_id != source_data_source_id:
                raise RuntimeError(
                    f'The "{relation_name}" relation on Task Allocations points '
                    f'to data source {target_data_source_id}, but it should point '
                    f'to {source_data_source_id} ({source_database_name}).'
                )

            if (
                not target_data_source_id
                and target_database_id
                and target_database_id != source_database_id
            ):
                raise RuntimeError(
                    f'The "{relation_name}" relation on Task Allocations points '
                    f'to database {target_database_id}, but it should point '
                    f'to {source_database_id} ({source_database_name}).'
                )

            continue

        print(
            f'Creating "{relation_name}" relation on Task Allocations '
            f'to {source_database_name}.'
        )

        notion(
            "PATCH",
            f"data_sources/{allocation_data_source_id}",
            json={
                "properties": {
                    relation_name: {
                        "relation": {
                            "data_source_id": source_data_source_id,
                            "single_property": {},
                        }
                    }
                }
            },
        )

        # Keep our local schema copy current so multiple missing relations can
        # be added in one run without depending on another GET response.
        existing_properties[relation_name] = {
            "type": "relation",
            "relation": {
                "data_source_id": source_data_source_id,
                "single_property": {},
            },
        }

        time.sleep(CREATE_REQUEST_DELAY)


def query_data_source(data_source_id):
    pages = []
    cursor = None

    while True:

        payload = {
            "page_size": 100,
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


def archive_page(page_id):
    notion(
        "PATCH",
        f"pages/{page_id}",
        json={
            "in_trash": True
        },
    )


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
    prop = page.get("properties", {}).get(
        property_name
    )

    if not prop or prop.get("type") != "title":
        return ""

    return "".join(
        item.get("plain_text", "")
        for item in prop.get("title", [])
    )


def checkbox_value(page, property_name):
    prop = page.get("properties", {}).get(
        property_name
    )

    if not prop or prop.get("type") != "checkbox":
        return False

    return bool(
        prop.get("checkbox", False)
    )


def number_value(page, property_name):
    prop = page.get("properties", {}).get(
        property_name
    )

    if not prop or prop.get("type") != "number":
        return None

    return prop.get("number")


def select_value(page, property_name):
    prop = page.get("properties", {}).get(
        property_name
    )

    if not prop or prop.get("type") != "select":
        return None

    option = prop.get("select")

    if not option:
        return None

    return option.get("name")


def relation_ids(page, property_name):
    prop = page.get("properties", {}).get(
        property_name
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
    prop = page.get("properties", {}).get(
        property_name
    )

    if not prop or prop.get("type") != "date":
        return None

    date = prop.get("date")

    if not date:
        return None

    start = date.get("start")
    end = date.get("end")

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
# SOURCE TASKS
# ============================================================

def read_tasks():
    tasks = []

    for database_name, relation_name in SOURCE_DATABASES.items():

        database_id = find_database(
            database_name
        )

        ensure_number_property(
            database_id,
            COMPLETED_UNITS_PROPERTY_NAME,
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
                "Task",
            ).strip()

            if not name:
                continue

            dependency_property = (
                page.get("properties", {})
                .get(DEPENDENCY_PROPERTY_NAME)
            )

            workload = number_value(
                page,
                "Workload",
            )

            completed_units = number_value(
                page,
                COMPLETED_UNITS_PROPERTY_NAME,
            ) or 0

            if workload is not None:
                completed_units = max(
                    0,
                    min(completed_units, workload),
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
                "continuous": checkbox_value(
                    page,
                    "Continuous",
                ),
                "completed": checkbox_value(
                    page,
                    "Completed",
                ),
                "hold": checkbox_value(
                    page,
                    "Hold",
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
    """Translate Completed Units into completed time using Time Needed."""
    total_minutes = task.get("minutes", 0)
    workload = task.get("workload")
    completed_units = task.get("completed_units", 0) or 0

    if total_minutes <= 0 or workload is None or workload <= 0:
        return 0

    completed_units = max(0, min(completed_units, workload))
    return total_minutes * (completed_units / workload)


def task_remaining_minutes(task):
    """Return Time Needed remaining after Completed Units."""
    return max(
        0,
        task.get("minutes", 0) - task_completed_minutes(task),
    )



def read_tasks():
    """Read active source tasks and normalize their scheduling information."""
    tasks = []

    for database_name, relation_name in SOURCE_DATABASES.items():
        database_id = find_database(database_name)

        ensure_number_property(
            database_id,
            COMPLETED_UNITS_PROPERTY_NAME,
        )

        data_source_id = get_data_source(database_id)
        pages = query_data_source(data_source_id)

        for page in pages:
            name = title_value(page, "Task").strip()
            if not name:
                continue

            workload = number_value(page, "Workload")
            completed_units = number_value(
                page, COMPLETED_UNITS_PROPERTY_NAME
            ) or 0

            if workload is not None:
                completed_units = max(
                    0, min(completed_units, workload)
                )

            unit = select_value(page, "Unit")
            completed = checkbox_value(page, "Completed")

            # A completed source task is never scheduled again.
            # If Completed Units reaches the full workload, normalize the
            # checkbox later in sync_master_completion().
            tasks.append({
                "page_id": page["id"],
                "task": name,
                "database": database_name,
                "relation_name": relation_name,
                "deadline": date_value(page, "Deadline"),
                "workload": workload,
                "completed_units": completed_units,
                "unit": unit,
                "priority": select_value(page, "Priority"),
                "completed": completed,
                "bump": checkbox_value(page, "Bump"),
                "dependencies": relation_ids(
                    page, DEPENDENCY_PROPERTY_NAME
                ),
                "minutes": workload_to_minutes(workload, unit),
            })

    print(f"Source tasks found: {len(tasks)}")
    return tasks


def task_completed_minutes(task):
    total_minutes = task.get("minutes", 0)
    workload = task.get("workload")
    completed_units = task.get("completed_units", 0) or 0

    if total_minutes <= 0 or workload is None or workload <= 0:
        return 0

    completed_units = max(0, min(completed_units, workload))
    return total_minutes * (completed_units / workload)


def task_remaining_minutes(task):
    return max(
        0,
        task.get("minutes", 0) - task_completed_minutes(task),
    )


def task_remaining_units(task):
    workload = task.get("workload")
    if workload is None:
        return 0

    return max(
        0,
        workload - (task.get("completed_units", 0) or 0),
    )


def deadline_target_date(task, now):
    """Return the preferred completion date.

    Normally the target is the day before the deadline. If that would require
    more than two hours/day, the deadline day is included.
    """
    deadline = task.get("deadline")
    if not deadline:
        return None

    deadline_day = deadline["start"].date()
    today = now.date()

    if deadline_day <= today:
        return today

    preferred = deadline_day - timedelta(days=1)
    days = max(1, (preferred - today).days + 1)
    remaining = task_remaining_minutes(task)

    if remaining / days > 120:
        return deadline_day

    return preferred


def available_workdays(start_date, target_date):
    if target_date < start_date:
        return 1
    # We deliberately count calendar days: the scheduler describes the
    # workload you need to get through, rather than assuming a fixed workweek.
    return max(1, (target_date - start_date).days + 1)


def required_daily_minutes(task, now):
    target = deadline_target_date(task, now)
    if target is None:
        return 0

    days = available_workdays(now.date(), target)
    return task_remaining_minutes(task) / days


def priority_weight(priority):
    return {
        "High": 0,
        "Medium": 1,
        "Low": 2,
    }.get(priority, 1)


def dependency_status(task, tasks_by_id):
    """Return (blocked, unmet_dependencies)."""
    unmet = []

    for dependency_id in task.get("dependencies", []):
        dependency = tasks_by_id.get(
            normalize_notion_id(dependency_id)
        )
        if dependency and not dependency.get("completed"):
            unmet.append(dependency["task"])

    return bool(unmet), unmet


def sort_key(task, now, tasks_by_id):
    remaining = task_remaining_minutes(task)
    deadline = task.get("deadline")
    blocked, _ = dependency_status(task, tasks_by_id)

    overdue = False
    due_today = False
    days_left = float("inf")

    if deadline:
        deadline_day = deadline["start"].date()
        days_left = (deadline_day - now.date()).days
        overdue = days_left < 0
        due_today = days_left == 0

    # Urgency is deadline/workload driven, with explicit Priority able to
    # elevate genuinely urgent work.
    if overdue:
        bucket = 0
    elif due_today:
        bucket = 1
    elif task.get("priority") == "High":
        bucket = 2
    elif deadline:
        daily = required_daily_minutes(task, now)
        if daily > 120:
            bucket = 2
        elif daily > 60:
            bucket = 3
        else:
            bucket = 4
    else:
        bucket = 6

    if blocked:
        bucket += 5

    return (
        bucket,
        priority_weight(task.get("priority")),
        days_left,
        -required_daily_minutes(task, now),
        task["task"].lower(),
    )


def allocation_units_for_task(task, now, rank):
    """Choose today's useful amount of work for the queue.

    The first pass is deadline-driven. Ahead-of-schedule work is deliberately
    modest so the visible queue remains manageable.
    """
    remaining_units = task_remaining_units(task)
    if remaining_units <= 0:
        return 0

    daily_minutes = required_daily_minutes(task, now)

    if daily_minutes > 0:
        # For urgent work, use the required daily amount. Otherwise cap
        # individual queue entries at roughly 60 minutes unless the task's
        # unit requires a larger indivisible amount.
        minutes = daily_minutes
        if daily_minutes <= 60 and rank >= 15:
            minutes = min(daily_minutes, 60)
        return max(
            1,
            min(
                remaining_units,
                minutes_to_units(minutes, task["unit"]),
            ),
        )

    # No-deadline / genuinely ahead-of-schedule work.
    minutes = 45 if rank >= 15 else 60
    return max(
        1,
        min(
            remaining_units,
            minutes_to_units(minutes, task["unit"]),
        ),
    )


# ============================================================
# TASK ALLOCATIONS
# ============================================================

ALLOCATION_PROPERTIES = {
    "Allocation": "number",
    "Completed Units": "number",
    "Bump": "checkbox",
    "Cancel": "checkbox",
    "Schedule Order": "number",
}


def allocation_title_property(database_id):
    source_id = get_data_source(database_id)
    source = notion("GET", f"data_sources/{source_id}")
    for name, prop in source.get("properties", {}).items():
        if prop.get("type") == "title":
            return name
    raise RuntimeError(
        "Task Allocations has no title property in its data source."
    )


def ensure_allocation_properties(database_id):
    """Ensure only the properties actively used by the new scheduler exist."""
    ensure_number_property(database_id, "Allocation")
    ensure_number_property(database_id, "Completed Units")

    source_id = get_data_source(database_id)
    source = notion("GET", f"data_sources/{source_id}")
    props = source.get("properties", {})

    for name, config in {
        "Bump": {"checkbox": {}},
        "Cancel": {"checkbox": {}},
    }.items():
        if name not in props:
            notion(
                "PATCH",
                f"data_sources/{source_id}",
                json={"properties": {name: config}},
            )
            time.sleep(CREATE_REQUEST_DELAY)
        elif props[name].get("type") != "checkbox":
            raise RuntimeError(
                f'"{name}" exists on Task Allocations but is not a checkbox.'
            )

    ensure_number_property(database_id, "Schedule Order")

    ensure_allocation_relation_properties(database_id)


def read_allocations():
    database_id = find_database("Task Allocations")
    data_source_id = get_data_source(database_id)
    pages = query_data_source(data_source_id)
    title_name = allocation_title_property(database_id)

    allocations = []

    for page in pages:
        source_links = {}
        for relation_name in SOURCE_DATABASES.values():
            ids = relation_ids(page, relation_name)
            if ids:
                source_links[relation_name] = ids

        allocations.append({
            "page_id": page["id"],
            "name": title_value(page, title_name),
            "source_links": source_links,
            "allocation": number_value(page, "Allocation") or 0,
            "completed_units": number_value(
                page, "Completed Units"
            ) or 0,
            "unit": select_value(page, "Unit"),
            "completed": checkbox_value(page, "Completed"),
            "bump": checkbox_value(page, "Bump"),
            "cancel": checkbox_value(page, "Cancel"),
            "schedule_order": number_value(page, "Schedule Order"),
        })

    return allocations


def allocation_task_id(allocation, tasks_by_id):
    normalized = {
        normalize_notion_id(task_id): task_id
        for task_id in tasks_by_id
    }

    matches = []
    for ids in allocation["source_links"].values():
        for raw_id in ids:
            task_id = normalized.get(normalize_notion_id(raw_id))
            if task_id and task_id not in matches:
                matches.append(task_id)

    return matches[0] if matches else None


def create_allocation(database_id, task, units, order):
    title_name = allocation_title_property(database_id)

    properties = {
        title_name: {
            "title": [
                {
                    "text": {
                        "content": task["task"]
                    }
                }
            ]
        },
        "Allocation": {"number": units},
        "Completed Units": {"number": 0},
        "Completed": {"checkbox": False},
        "Bump": {"checkbox": False},
        "Cancel": {"checkbox": False},
        "Schedule Order": {"number": order},
        "Unit": {
            "select": {
                "name": task["unit"]
            }
        },
        task["relation_name"]: {
            "relation": [
                {"id": task["page_id"]}
            ]
        },
    }

    notion(
        "POST",
        "pages",
        json={
            "parent": {"database_id": database_id},
            "properties": properties,
        },
    )


def update_allocation(page_id, properties):
    notion(
        "PATCH",
        f"pages/{page_id}",
        json={"properties": properties},
    )


def sync_master_completion(tasks):
    """Make Completed authoritative at the master-task level."""
    for task in tasks:
        workload = task.get("workload")
        completed_units = task.get("completed_units", 0) or 0

        if workload is None or workload <= 0:
            continue

        should_be_completed = completed_units >= workload

        if should_be_completed and not task["completed"]:
            notion(
                "PATCH",
                f"pages/{task['page_id']}",
                json={
                    "properties": {
                        "Completed": {"checkbox": True}
                    }
                },
            )


def apply_bumps(tasks, tasks_by_id, now):
    """Move checked Bump tasks down exactly three queue positions.

    Bump is an action: after it is consumed, the checkbox is reset immediately.
    """
    ordered = sorted(
        tasks,
        key=lambda t: sort_key(t, now, tasks_by_id),
    )

    for task in list(ordered):
        if not task.get("bump"):
            continue

        try:
            index = ordered.index(task)
        except ValueError:
            continue

        new_index = min(len(ordered) - 1, index + 3)
        ordered.pop(index)
        ordered.insert(new_index, task)

        notion(
            "PATCH",
            f"pages/{task['page_id']}",
            json={
                "properties": {
                    "Bump": {"checkbox": False}
                }
            },
        )
        task["bump"] = False

    return ordered


def process_allocation_actions(allocations, tasks_by_id):
    """Propagate Daily Plan completion/cancellation/partial progress."""
    for allocation in allocations:
        task_id = allocation_task_id(allocation, tasks_by_id)
        if not task_id:
            continue

        task = tasks_by_id[task_id]

        if allocation["cancel"]:
            notion(
                "PATCH",
                f"pages/{task_id}",
                json={
                    "properties": {
                        "Completed": {"checkbox": False}
                    }
                },
            )
            update_allocation(
                allocation["page_id"],
                {"Cancel": {"checkbox": False}},
            )
            # Cancellation is represented by removing this allocation from
            # the active queue; the master checkbox remains available for
            # future explicit cancellation-property support.
            continue

        completed_units = allocation.get("completed_units", 0) or 0
        if completed_units > 0:
            new_total = min(
                task["workload"],
                task["completed_units"] + completed_units,
            )

            notion(
                "PATCH",
                f"pages/{task_id}",
                json={
                    "properties": {
                        "Completed Units": {
                            "number": new_total
                        },
                        "Completed": {
                            "checkbox": (
                                new_total >= task["workload"]
                            )
                        },
                    }
                },
            )

        if allocation["completed"]:
            notion(
                "PATCH",
                f"pages/{task_id}",
                json={
                    "properties": {
                        "Completed": {"checkbox": True},
                        "Completed Units": {
                            "number": task["workload"]
                        },
                    }
                },
            )


def rebuild_queue(tasks):
    database_id = find_database("Task Allocations")
    allocations = read_allocations()
    tasks_by_id = {
        normalize_notion_id(task["page_id"]): task
        for task in tasks
    }

    process_allocation_actions(allocations, tasks_by_id)

    # Refresh task data after actions have propagated.
    tasks = read_tasks()
    tasks_by_id = {
        normalize_notion_id(task["page_id"]): task
        for task in tasks
    }

    sync_master_completion(tasks)

    active = [
        task for task in tasks
        if not task["completed"]
        and task_remaining_minutes(task) > 0
    ]

    now = datetime.now(TZ)
    ordered = apply_bumps(active, tasks_by_id, now)

    # Because Task Allocations was intentionally emptied, build a clean queue.
    # If allocations already exist, archive them and rebuild so Schedule Order
    # is deterministic and stale rows cannot survive.
    for allocation in read_allocations():
        archive_page(allocation["page_id"])

    for index, task in enumerate(ordered, start=1):
        units = allocation_units_for_task(task, now, index)
        if units <= 0:
            continue
        create_allocation(database_id, task, units, index)
        time.sleep(CREATE_REQUEST_DELAY)

    print(f"Queue rebuilt: {min(len(ordered), len(ordered))} active tasks.")


def main():
    print("=" * 50)
    print("NOTION DAILY QUEUE SCHEDULER")
    print("=" * 50)

    tasks = read_tasks()

    allocations_database_id = find_database("Task Allocations")
    ensure_allocation_properties(allocations_database_id)

    rebuild_queue(tasks)

    print("Done.")


if __name__ == "__main__":
    main()
