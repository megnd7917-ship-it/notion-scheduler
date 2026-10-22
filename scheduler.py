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


DEPENDENCY_PROPERTY_NAME = "Blocked by"
COMPLETED_UNITS_PROPERTY_NAME = "Completed Units"

PFS_TASK_NAME = "PFS weekly hours"
PFS_DATABASE_NAME = "PFS To-Do"
CREATE_REQUEST_DELAY = 0.5
MAX_RATE_LIMIT_RETRIES = 7
# Notion's public API limit is roughly 3 requests/second. Leave headroom so
# sequential database/schema lookups do not arrive in bursts.
MIN_REQUEST_INTERVAL_SECONDS = 0.4
_LAST_REQUEST_AT = 0.0
_DATABASE_ID_CACHE = {}
_DATABASE_OBJECT_CACHE = {}
_DATA_SOURCE_ID_CACHE = {}
_DATA_SOURCE_SCHEMA_CACHE = {}



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


def format_task_estimate(minutes):
    """Format remaining estimated work compactly for the Daily Plan title."""
    minutes = max(0, int(round(minutes)))
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hr" if hours == 1 else f"{hours} hrs"
    if minutes >= 60:
        hours = minutes // 60
        remainder = minutes % 60
        return f"{hours} hr {remainder} mins"
    return f"{minutes} min" if minutes == 1 else f"{minutes} mins"


# ============================================================
# NOTION API
# ============================================================

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def notion(method, path, **kwargs):
    """Call Notion with global pacing and robust 429 retry handling."""
    global _LAST_REQUEST_AT
    url = f"https://api.notion.com/v1/{path}"

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        elapsed = time.monotonic() - _LAST_REQUEST_AT
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

        response = requests.request(
            method,
            url,
            headers=HEADERS,
            timeout=45,
            **kwargs,
        )
        _LAST_REQUEST_AT = time.monotonic()

        if response.ok:
            if not response.content:
                return {}
            return response.json()

        if response.status_code == 429:
            # Notion returns retry_after in top-level additional_data. Some
            # responses also provide Retry-After as an HTTP header.
            retry_after = response.headers.get("Retry-After")
            try:
                data = response.json()
                retry_after = retry_after or data.get("additional_data", {}).get("retry_after")
                retry_after = float(retry_after) if retry_after is not None else min(5 * (2 ** attempt), 60)
            except (ValueError, TypeError):
                retry_after = min(5 * (2 ** attempt), 60)

            if attempt >= MAX_RATE_LIMIT_RETRIES:
                raise RuntimeError(
                    "Notion rate limit persisted after "
                    f"{MAX_RATE_LIMIT_RETRIES} retries: {response.text}"
                )

            wait_seconds = max(1.0, retry_after) + 0.25
            print(
                f"Notion rate limit reached. Waiting {wait_seconds:g} seconds "
                f"before retry {attempt + 1}/{MAX_RATE_LIMIT_RETRIES}..."
            )
            time.sleep(wait_seconds)
            continue

        raise RuntimeError(
            f"Notion API error {response.status_code}: {response.text}"
        )

    raise RuntimeError("Notion API request failed.")


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
    if name in _DATABASE_ID_CACHE:
        return _DATABASE_ID_CACHE[name]

    for obj in search_all(name):

        if obj.get("object") == "database":

            title = "".join(
                item.get("plain_text", "")
                for item in obj.get("title", [])
            ).strip()

            if title == name:
                _DATABASE_ID_CACHE[name] = obj["id"]
                _DATABASE_OBJECT_CACHE[obj["id"]] = obj
                return obj["id"]

        elif obj.get("object") == "data_source":

            database_id = (
                obj.get("parent", {})
                .get("database_id")
            )

            if not database_id:
                continue

            database = _DATABASE_OBJECT_CACHE.get(database_id)
            if database is None:
                database = notion("GET", f"databases/{database_id}")
                _DATABASE_OBJECT_CACHE[database_id] = database

            title = "".join(
                item.get("plain_text", "")
                for item in database.get("title", [])
            ).strip()

            if title == name:
                _DATABASE_ID_CACHE[name] = database_id
                return database_id

    raise RuntimeError(
        f'Could not find Notion database "{name}".'
    )


def get_data_source(database_id):
    if database_id in _DATA_SOURCE_ID_CACHE:
        return _DATA_SOURCE_ID_CACHE[database_id]

    data = _DATABASE_OBJECT_CACHE.get(database_id)
    if data is None or not data.get("data_sources"):
        data = notion("GET", f"databases/{database_id}")
        _DATABASE_OBJECT_CACHE[database_id] = data

    sources = data.get("data_sources", [])

    if not sources:
        raise RuntimeError(
            f"No data source found for database "
            f"{database_id}."
        )

    _DATA_SOURCE_ID_CACHE[database_id] = sources[0]["id"]
    return sources[0]["id"]


def ensure_database_property(database_id, property_name, property_type, config):
    """Ensure a property exists, using a cached authoritative data-source schema."""
    data_source_id = get_data_source(database_id)
    properties = _DATA_SOURCE_SCHEMA_CACHE.get(data_source_id)

    if properties is None:
        data_source = notion("GET", f"data_sources/{data_source_id}")
        properties = dict(data_source.get("properties", {}))
        _DATA_SOURCE_SCHEMA_CACHE[data_source_id] = properties

    existing = properties.get(property_name)
    if existing:
        if existing.get("type") != property_type:
            raise RuntimeError(
                f'The "{property_name}" property exists but is not a '
                f'{property_type} property.'
            )
        return

    print(
        f'Creating "{property_name}" {property_type} property in '
        f'data source {data_source_id}.'
    )
    notion(
        "PATCH",
        f"data_sources/{data_source_id}",
        json={"properties": {property_name: config}},
    )
    # Update the local schema only after Notion confirms the write succeeded.
    properties[property_name] = {"type": property_type}


def ensure_number_property(database_id, property_name):
    """Create a number property on a database if it does not already exist."""
    ensure_database_property(
        database_id,
        property_name,
        "number",
        {"number": {"format": "number"}},
    )


def ensure_checkbox_property(database_id, property_name):
    """Create a checkbox property on a database if it does not already exist."""
    ensure_database_property(
        database_id,
        property_name,
        "checkbox",
        {"checkbox": {}},
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

    existing_properties = _DATA_SOURCE_SCHEMA_CACHE.get(allocation_data_source_id)
    if existing_properties is None:
        allocation_source = notion(
            "GET",
            f"data_sources/{allocation_data_source_id}",
        )
        existing_properties = dict(allocation_source.get("properties", {}))
        _DATA_SOURCE_SCHEMA_CACHE[allocation_data_source_id] = existing_properties

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
        existing_properties[relation_name] = {
            "type": "relation",
            "relation": {"data_source_id": source_data_source_id},
        }

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
        database_id = find_database(database_name)

        # These are the only scheduler-managed source properties.
        ensure_number_property(
            database_id,
            COMPLETED_UNITS_PROPERTY_NAME,
        )
        ensure_checkbox_property(database_id, "Bump")
        ensure_checkbox_property(database_id, "Canceled")

        data_source_id = get_data_source(database_id)

        for page in query_data_source(data_source_id):
            name = title_value(page, "Task").strip()
            if not name:
                continue

            workload = number_value(page, "Workload")
            completed_units = number_value(
                page,
                COMPLETED_UNITS_PROPERTY_NAME,
            ) or 0

            if workload is not None:
                completed_units = max(
                    0,
                    min(completed_units, workload),
                )

            unit = select_value(page, "Unit")

            # One-time compatibility migration: if an older run created a
            # lowercase/spelling-variant cancellation checkbox and it is
            # checked, preserve that state in the canonical "Canceled" field.
            canceled = checkbox_value(page, "Canceled")
            legacy_canceled = (
                checkbox_value(page, "canceled")
                or checkbox_value(page, "cancelled")
            )
            if legacy_canceled and not canceled:
                notion(
                    "PATCH",
                    f"pages/{page['id']}",
                    json={"properties": {"Canceled": {"checkbox": True}}},
                )
                canceled = True

            tasks.append({
                "page_id": page["id"],
                "task": name,
                "database": database_name,
                "relation_name": relation_name,
                "source_label": SOURCE_LABELS[database_name],
                "deadline": date_value(page, "Deadline"),
                "workload": workload,
                "completed_units": completed_units,
                "unit": unit,
                "priority": select_value(page, "Priority"),
                "completed": checkbox_value(page, "Completed"),
                "bump": checkbox_value(page, "Bump"),
                "canceled": canceled,
                "dependencies": relation_ids(page, DEPENDENCY_PROPERTY_NAME),
                "minutes": workload_to_minutes(workload, unit),
            })

    print(f"Source tasks found: {len(tasks)}")
    return tasks


def task_completed_minutes(task):
    total_minutes = task.get("minutes", 0)
    workload = task.get("workload")
    completed_units = task.get("completed_units", 0) or 0

    if total_minutes <= 0 or not workload or workload <= 0:
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
    """Return the preferred finish date without imposing daily capacity."""
    deadline = task.get("deadline")
    if not deadline:
        return None
    return deadline["start"].date()


def required_daily_minutes(task, now):
    """A workload-pressure signal used for ranking, not capacity planning."""
    deadline = task.get("deadline")
    if not deadline:
        return 0
    days_left = max(1, (deadline["start"].date() - now.date()).days)
    return task_remaining_minutes(task) / days_left


def priority_weight(priority):
    return {"High": 0, "Medium": 1, "Low": 2}.get(priority, 1)


def dependency_status(task, tasks_by_id):
    unmet = []

    for dependency_id in task.get("dependencies", []):
        dependency = tasks_by_id.get(normalize_notion_id(dependency_id))
        if dependency and not dependency.get("completed") and not dependency.get("canceled"):
            unmet.append(dependency["task"])

    return bool(unmet), unmet


def sort_key(task, now, tasks_by_id):
    """Dynamic whole-task ordering: deadline urgency first, workload pressure second.

    No capacity calculations or daily task splitting are performed. Tasks are
    ranked again on every run, so their positions change as deadlines approach
    and completed units reduce the remaining workload.
    """
    deadline = task.get("deadline")
    blocked, _ = dependency_status(task, tasks_by_id)
    remaining_minutes = task_remaining_minutes(task)

    if deadline:
        days_left = (deadline["start"].date() - now.date()).days
        if days_left < 0:
            urgency_bucket = 0       # overdue
        elif days_left == 0:
            urgency_bucket = 1       # due today
        elif days_left == 1:
            urgency_bucket = 2       # due tomorrow
        elif days_left <= 3:
            urgency_bucket = 3       # due in 2–3 days
        elif days_left <= 7:
            urgency_bucket = 4       # due this week
        elif days_left <= 14:
            urgency_bucket = 5       # due in the next two weeks
        else:
            urgency_bucket = 6       # distant deadline
        workload_pressure = remaining_minutes / max(days_left, 1)
    else:
        days_left = float("inf")
        urgency_bucket = 7           # no deadline
        workload_pressure = 0

    # Blocked tasks remain visible, but move below tasks that can be acted on.
    blocked_penalty = 1 if blocked else 0

    return (
        urgency_bucket,
        blocked_penalty,
        -workload_pressure,
        priority_weight(task.get("priority")),
        -remaining_minutes,
        task["task"].lower(),
    )


def allocation_units_for_task(task, now, rank=None):
    """Keep the entire remaining task in one row; do not split into daily chunks."""
    return task_remaining_units(task)


# ============================================================
# TASK ALLOCATIONS
# ============================================================

def allocation_title_property(database_id):
    source_id = get_data_source(database_id)
    source = notion("GET", f"data_sources/{source_id}")

    for name, prop in source.get("properties", {}).items():
        if prop.get("type") == "title":
            return name

    raise RuntimeError("Task Allocations has no title property.")


def ensure_allocation_properties(database_id):
    ensure_number_property(database_id, "Allocation")
    ensure_number_property(database_id, "Completed Units")
    ensure_number_property(database_id, "Schedule Order")

    ensure_checkbox_property(database_id, "Completed")
    ensure_checkbox_property(database_id, "Bump")
    ensure_checkbox_property(database_id, "Cancel")

    ensure_allocation_relation_properties(database_id)


def read_allocations():
    database_id = find_database("Task Allocations")
    data_source_id = get_data_source(database_id)
    title_name = allocation_title_property(database_id)

    allocations = []

    for page in query_data_source(data_source_id):
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
            "completed_units": number_value(page, "Completed Units") or 0,
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


def display_title(task):
    """Daily Plan title: original task, source label, and estimated remaining time."""
    base = task["task"].strip()
    source = task["source_label"]
    estimate = format_task_estimate(task_remaining_minutes(task))
    return f"{base} - {source} - {estimate}"


def create_allocation(database_id, task, units, order):
    title_name = allocation_title_property(database_id)

    properties = {
        title_name: {
            "title": [{
                "text": {
                    "content": display_title(task)
                }
            }]
        },
        "Allocation": {"number": units},
        "Completed Units": {"number": 0},
        "Completed": {"checkbox": False},
        "Bump": {"checkbox": False},
        "Cancel": {"checkbox": False},
        "Schedule Order": {"number": order},
        "Unit": {"select": {"name": task["unit"]}},
        task["relation_name"]: {
            "relation": [{"id": task["page_id"]}]
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
    for task in tasks:
        workload = task.get("workload")
        completed_units = task.get("completed_units", 0) or 0

        if workload is None or workload <= 0:
            continue

        if completed_units >= workload and not task.get("completed"):
            notion(
                "PATCH",
                f"pages/{task['page_id']}",
                json={
                    "properties": {
                        "Completed": {"checkbox": True}
                    }
                },
            )


def apply_bumps(ordered):
    """Move each checked Bump action down exactly three positions."""
    result = list(ordered)

    for task in list(result):
        if not task.get("bump"):
            continue

        old_index = result.index(task)
        new_index = min(len(result) - 1, old_index + 3)

        result.pop(old_index)
        result.insert(new_index, task)

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

    return result


def process_allocation_actions(allocations, tasks_by_id):
    """Propagate Daily Plan actions into source To-Do databases."""
    for allocation in allocations:
        task_id = allocation_task_id(allocation, tasks_by_id)
        if not task_id:
            continue

        task = tasks_by_id[task_id]

        # Bump from the Daily Plan is forwarded to the source task. The normal
        # queue-ordering pass then moves it down three positions and clears it.
        if allocation.get("bump"):
            notion(
                "PATCH",
                f"pages/{task_id}",
                json={"properties": {"Bump": {"checkbox": True}}},
            )
            update_allocation(
                allocation["page_id"],
                {"Bump": {"checkbox": False}},
            )

        # Cancel marks the source task canceled and removes its active row.
        if allocation["cancel"]:
            notion(
                "PATCH",
                f"pages/{task_id}",
                json={
                    "properties": {
                        "Canceled": {"checkbox": True},
                        "Bump": {"checkbox": False},
                    }
                },
            )

            update_allocation(
                allocation["page_id"],
                {"Cancel": {"checkbox": False}},
            )
            continue

        # Completed Units on the Daily Plan is a one-time progress increment.
        # Reset it after crediting it so the same work is not counted twice.
        completed_units = allocation.get("completed_units", 0) or 0

        if completed_units > 0 and task.get("workload") is not None:
            new_total = min(
                task["workload"],
                task["completed_units"] + completed_units,
            )

            notion(
                "PATCH",
                f"pages/{task_id}",
                json={
                    "properties": {
                        "Completed Units": {"number": new_total},
                        "Completed": {
                            "checkbox": new_total >= task["workload"]
                        },
                    }
                },
            )

            update_allocation(
                allocation["page_id"],
                {"Completed Units": {"number": 0}},
            )

        # Checking Completed on the Daily Plan finishes the whole source task.
        if allocation["completed"]:
            completed_value = (
                task["workload"]
                if task.get("workload") is not None
                else task.get("completed_units", 0)
            )

            notion(
                "PATCH",
                f"pages/{task_id}",
                json={
                    "properties": {
                        "Completed": {"checkbox": True},
                        "Completed Units": {"number": completed_value},
                    }
                },
            )


def reconcile_allocations(tasks, allocations):
    """Incrementally reconcile Task Allocations without rebuilding the database."""
    database_id = find_database("Task Allocations")

    tasks_by_id = {
        normalize_notion_id(task["page_id"]): task
        for task in tasks
    }

    now = datetime.now(TZ)

    active = [
        task
        for task in tasks
        if not task.get("completed")
        and not task.get("canceled")
        and task_remaining_minutes(task) > 0
    ]

    ordered = sorted(
        active,
        key=lambda task: sort_key(task, now, tasks_by_id),
    )

    ordered = apply_bumps(ordered)

    desired_ids = {
        normalize_notion_id(task["page_id"])
        for task in ordered
    }

    # Match each existing allocation to its source task. Keep one allocation
    # per active task and archive only genuine duplicates/orphans/completed rows.
    matched = {}

    for allocation in allocations:
        task_id = allocation_task_id(allocation, tasks_by_id)

        if not task_id:
            print(
                f'Orphaned allocation "{allocation["name"]}" will be archived.'
            )
            archive_page(allocation["page_id"])
            continue

        task = tasks_by_id[task_id]

        if (
            task.get("completed")
            or task.get("canceled")
            or task_remaining_minutes(task) <= 0
        ):
            archive_page(allocation["page_id"])
            continue

        if task_id in matched:
            print(
                f'Duplicate allocation "{allocation["name"]}" will be archived.'
            )
            archive_page(allocation["page_id"])
            continue

        matched[task_id] = allocation

    # Update existing rows in place; each row represents the whole remaining task.
    for index, task in enumerate(ordered, start=1):
        task_id = normalize_notion_id(task["page_id"])
        units = allocation_units_for_task(task, now, index)

        if units <= 0:
            continue

        desired_title = display_title(task)
        allocation = matched.get(task_id)

        if allocation:
            title_name = allocation_title_property(database_id)
            properties = {
                "Allocation": {"number": units},
                "Schedule Order": {"number": index},
                "Unit": {"select": {"name": task["unit"]}},
            }

            if allocation["name"] != desired_title:
                properties[title_name] = {
                    "title": [{
                        "text": {"content": desired_title}
                    }]
                }

            update_allocation(
                allocation["page_id"],
                properties,
            )
        else:
            print(
                f'Creating allocation: "{desired_title}"'
            )
            create_allocation(
                database_id,
                task,
                units,
                index,
            )
            time.sleep(CREATE_REQUEST_DELAY)

    # Any surviving allocation not represented by an active task is stale.
    for task_id, allocation in matched.items():
        if task_id not in desired_ids:
            archive_page(allocation["page_id"])

    print(
        f"Task Allocations reconciled: {len(ordered)} active tasks."
    )


def main():
    print("=" * 60)
    print("NOTION DAILY QUEUE SCHEDULER")
    print("=" * 60)

    tasks = read_tasks()

    allocations_database_id = find_database("Task Allocations")
    ensure_allocation_properties(allocations_database_id)

    allocations = read_allocations()
    tasks_by_id = {
        normalize_notion_id(task["page_id"]): task
        for task in tasks
    }

    # User actions in the Daily Plan are always processed before recalculation.
    process_allocation_actions(allocations, tasks_by_id)

    # Refresh source data after those actions so the next queue reflects the
    # actual current progress/completion/cancellation state.
    tasks = read_tasks()
    sync_master_completion(tasks)
    tasks = read_tasks()
    allocations = read_allocations()

    reconcile_allocations(tasks, allocations)

    print("Done.")


if __name__ == "__main__":
    main()
