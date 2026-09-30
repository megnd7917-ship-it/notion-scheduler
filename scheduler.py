import hashlib
import json
import os
import time
from datetime import datetime, timedelta, date
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
BUMP_PROPERTY_NAME = "Bump"
CANCEL_PROPERTY_NAME = "Cancel"
CANCELLED_PROPERTY_NAME = "Cancelled"

SCHEDULE_ORDER_PROPERTY = "Schedule Order"
ALLOCATION_PROPERTY = "Allocation"
UNIT_PROPERTY = "Unit"
COMPLETED_PROPERTY = "Completed"

# A task normally aims to be finished one calendar day before its deadline.
# If that would require more than this much work per day, the deadline day
# itself becomes available too.
TARGET_DAILY_LIMIT_MINUTES = 120

# Deadline-free tasks remain visible, but start after the deadline-driven queue.
UNDATED_START_ORDER = 18

# Bump means "move this task down three places for now."
BUMP_DISTANCE = 3

CREATE_REQUEST_DELAY = 0.5
MAX_RATE_LIMIT_RETRIES = 5

STATE_FILE = os.environ.get("SCHEDULER_STATE_FILE", ".scheduler_state.json")

# The title property in Task Allocations may have been renamed in Notion.
# We discover its actual property name from the database schema instead of
# requiring it to be literally called "Name".
ALLOCATION_TITLE_PROPERTY_NAME = None

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

MINUTES_PER_UNIT = {
    "Pages": 5,
    "LSAT Questions": 3,
    "Questions": 3,
    "Papers": 10,
    "Hours": 60,
    "Minutes": 1,
}

PRIORITY_RANK = {"High": 0, "Medium": 1, "Low": 2, None: 1}

# ============================================================
# BASIC HELPERS
# ============================================================

def workload_to_minutes(workload, unit):
    if workload is None or not unit:
        return 0
    if unit not in MINUTES_PER_UNIT:
        raise RuntimeError(f'Unknown workload unit "{unit}".')
    return workload * MINUTES_PER_UNIT[unit]


def minutes_to_units(minutes, unit):
    if unit not in MINUTES_PER_UNIT:
        raise RuntimeError(f'Unknown workload unit "{unit}".')
    return minutes / MINUTES_PER_UNIT[unit]


def format_minutes(minutes):
    minutes = int(round(minutes))
    hours, remainder = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append("1 hour" if hours == 1 else f"{hours} hours")
    if remainder:
        parts.append("1 minute" if remainder == 1 else f"{remainder} minutes")
    return " ".join(parts) if parts else "0 minutes"


def format_allocation(minutes, unit):
    if unit in ("Hours", "Minutes"):
        return format_minutes(minutes)
    amount = minutes_to_units(minutes, unit)
    text = str(int(amount)) if amount == int(amount) else f"{amount:g}"
    return f"{text} {unit}"


def normalize_notion_id(value):
    return str(value).replace("-", "").strip().lower() if value else None


def stable_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()

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
        response = requests.request(method, url, headers=HEADERS, **kwargs)
        if response.ok:
            return response.json() if response.content else {}
        if response.status_code == 429:
            if attempt >= MAX_RATE_LIMIT_RETRIES:
                raise RuntimeError(f"Notion rate limit persisted: {response.text}")
            time.sleep(3)
            continue
        raise RuntimeError(f"Notion API error {response.status_code}: {response.text}")
    raise RuntimeError("Notion API request failed.")


def search_all(query):
    results = []
    cursor = None
    while True:
        payload = {"query": query, "page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = notion("POST", "search", json=payload)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            return results
        cursor = data.get("next_cursor")


def find_database(name):
    for obj in search_all(name):
        if obj.get("object") == "database":
            title = "".join(x.get("plain_text", "") for x in obj.get("title", [])).strip()
            if title == name:
                return obj["id"]
        elif obj.get("object") == "data_source":
            database_id = obj.get("parent", {}).get("database_id")
            if not database_id:
                continue
            database = notion("GET", f"databases/{database_id}")
            title = "".join(x.get("plain_text", "") for x in database.get("title", [])).strip()
            if title == name:
                return database_id
    raise RuntimeError(f'Could not find Notion database "{name}".')


def get_data_source(database_id):
    data = notion("GET", f"databases/{database_id}")
    sources = data.get("data_sources", [])
    if not sources:
        raise RuntimeError(f"No data source found for database {database_id}.")
    return sources[0]["id"]


def query_data_source(data_source_id):
    pages = []
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = notion("POST", f"data_sources/{data_source_id}/query", json=payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data.get("next_cursor")


def archive_page(page_id):
    notion("PATCH", f"pages/{page_id}", json={"in_trash": True})

# ============================================================
# PROPERTY HELPERS
# ============================================================

def title_value(page, property_name):
    prop = page.get("properties", {}).get(property_name)
    if not prop or prop.get("type") != "title":
        return ""
    return "".join(x.get("plain_text", "") for x in prop.get("title", []))


def checkbox_value(page, property_name):
    prop = page.get("properties", {}).get(property_name)
    return bool(prop and prop.get("type") == "checkbox" and prop.get("checkbox", False))


def number_value(page, property_name):
    prop = page.get("properties", {}).get(property_name)
    if not prop or prop.get("type") != "number":
        return None
    return prop.get("number")


def select_value(page, property_name):
    prop = page.get("properties", {}).get(property_name)
    if not prop or prop.get("type") != "select":
        return None
    option = prop.get("select")
    return option.get("name") if option else None


def relation_ids(page, property_name):
    prop = page.get("properties", {}).get(property_name)
    if not prop or prop.get("type") != "relation":
        return []
    return [item["id"] for item in prop.get("relation", [])]


def parse_datetime(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def date_value(page, property_name):
    prop = page.get("properties", {}).get(property_name)
    if not prop or prop.get("type") != "date" or not prop.get("date"):
        return None
    start = prop["date"].get("start")
    end = prop["date"].get("end")
    if not start:
        return None
    return {
        "start": parse_datetime(start),
        "end": parse_datetime(end) if end else None,
    }

# ============================================================
# SOURCE TASKS
# ============================================================

def read_tasks():
    tasks = []
    for database_name, relation_name in SOURCE_DATABASES.items():
        database_id = find_database(database_name)
        data_source_id = get_data_source(database_id)
        for page in query_data_source(data_source_id):
            name = title_value(page, "Task").strip()
            if not name:
                continue

            workload = number_value(page, "Workload")
            completed_units = number_value(page, COMPLETED_UNITS_PROPERTY_NAME) or 0
            if workload is not None:
                completed_units = max(0, min(completed_units, workload))

            unit = select_value(page, "Unit")
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
                "completed": checkbox_value(page, "Completed"),
                "cancelled": checkbox_value(page, CANCELLED_PROPERTY_NAME),
                "dependencies": relation_ids(page, DEPENDENCY_PROPERTY_NAME),
                "minutes": workload_to_minutes(workload, unit),
            })
    print(f"Source tasks found: {len(tasks)}")
    return tasks


def task_completed_minutes(task):
    if task["minutes"] <= 0 or not task.get("workload"):
        return 0
    return task["minutes"] * (task["completed_units"] / task["workload"])


def task_remaining_minutes(task):
    return max(0, task["minutes"] - task_completed_minutes(task))


def update_source_task(page_id, properties):
    notion("PATCH", f"pages/{page_id}", json={"properties": properties})

# ============================================================
# TASK ALLOCATIONS
# ============================================================

def read_allocations():
    database_id = find_database("Task Allocations")
    data_source_id = get_data_source(database_id)
    pages = query_data_source(data_source_id)
    allocations = []
    for page in pages:
        source_links = {}
        for relation_name in SOURCE_DATABASES.values():
            ids = relation_ids(page, relation_name)
            if ids:
                source_links[relation_name] = ids

        allocations.append({
            "page_id": page["id"],
            "name": title_value(page, ALLOCATION_TITLE_PROPERTY_NAME),
            "source_links": source_links,
            "allocation": number_value(page, ALLOCATION_PROPERTY) or 0,
            "unit": select_value(page, UNIT_PROPERTY),
            "completed": checkbox_value(page, COMPLETED_PROPERTY),
            "bump": checkbox_value(page, BUMP_PROPERTY_NAME),
            "cancel": checkbox_value(page, CANCEL_PROPERTY_NAME),
            "completed_units": number_value(page, COMPLETED_UNITS_PROPERTY_NAME) or 0,
            "schedule_order": number_value(page, SCHEDULE_ORDER_PROPERTY) or 0,
        })
    return allocations


def allocation_task_id(allocation, tasks_by_id):
    normalized = {normalize_notion_id(k): k for k in tasks_by_id}
    matches = []
    for ids in allocation["source_links"].values():
        for raw_id in ids:
            task_id = normalized.get(normalize_notion_id(raw_id))
            if task_id:
                matches.append(task_id)
    return matches[0] if matches else None


def allocation_minutes(allocation, task):
    return workload_to_minutes(allocation["allocation"], allocation.get("unit") or task["unit"])


def sync_daily_completion_to_master(tasks, allocations):
    """Make Daily Plan completion authoritative for the linked master task.

    A completed allocation means the underlying master task is finished. Its
    Completed Units are also brought to the full workload when possible.
    """
    tasks_by_id = {task["page_id"]: task for task in tasks}
    changed = 0
    for allocation in allocations:
        if not allocation["completed"]:
            continue
        task_id = allocation_task_id(allocation, tasks_by_id)
        if not task_id:
            continue
        task = tasks_by_id[task_id]
        if task["completed"]:
            continue
        properties = {COMPLETED_PROPERTY: {"checkbox": True}}
        if task.get("workload") is not None:
            properties[COMPLETED_UNITS_PROPERTY_NAME] = {"number": task["workload"]}
        update_source_task(task_id, properties)
        task["completed"] = True
        task["completed_units"] = task.get("workload") or task["completed_units"]
        changed += 1
        print(f'Completed master task from Daily Plan: "{task["task"]}"')
    return changed


def sync_partial_units_to_master(tasks, allocations, state):
    """Apply newly entered Daily Plan Completed Units to the master task.

    Allocation Completed Units represents progress made during that Daily Plan
    entry. The state file prevents the same entry from being counted twice.
    """
    tasks_by_id = {task["page_id"]: task for task in tasks}
    synced = state.setdefault("synced_allocation_units", {})
    changed = 0

    for allocation in allocations:
        if allocation["completed"]:
            continue
        units = allocation.get("completed_units", 0) or 0
        if units <= 0:
            continue
        task_id = allocation_task_id(allocation, tasks_by_id)
        if not task_id:
            continue
        task = tasks_by_id[task_id]
        previous = float(synced.get(allocation["page_id"], 0))
        delta = units - previous
        if delta <= 0:
            continue
        new_total = task["completed_units"] + delta
        if task.get("workload") is not None:
            new_total = min(new_total, task["workload"])

        properties = {COMPLETED_UNITS_PROPERTY_NAME: {"number": new_total}}
        if task.get("workload") is not None and new_total >= task["workload"]:
            properties[COMPLETED_PROPERTY] = {"checkbox": True}
            task["completed"] = True

        update_source_task(task_id, properties)
        task["completed_units"] = new_total
        synced[allocation["page_id"]] = units
        changed += 1
        print(f'Applied {delta:g} {task["unit"]} of progress to "{task["task"]}"')
    return changed

# ============================================================
# DEPENDENCIES
# ============================================================

def build_dependency_graph(tasks):
    tasks_by_id = {task["page_id"]: task for task in tasks}
    normalized = {normalize_notion_id(k): k for k in tasks_by_id}
    dependents = {task["page_id"]: set() for task in tasks}

    for task in tasks:
        valid = set()
        for raw_id in task["dependencies"]:
            dep_id = normalized.get(normalize_notion_id(raw_id))
            if not dep_id or dep_id == task["page_id"]:
                continue
            valid.add(dep_id)
            dependents[dep_id].add(task["page_id"])
        task["dependencies"] = sorted(valid)

    visiting = set()
    visited = set()

    def visit(task_id, path):
        if task_id in visiting:
            cycle = path[path.index(task_id):] + [task_id]
            names = [tasks_by_id[x]["task"] for x in cycle]
            raise RuntimeError("Circular dependency detected: " + " -> ".join(names))
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep_id in tasks_by_id[task_id]["dependencies"]:
            visit(dep_id, path + [dep_id])
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task["page_id"], [task["page_id"]])
    return tasks_by_id, dependents


def dependency_blocked(task, tasks_by_id):
    for dep_id in task["dependencies"]:
        dependency = tasks_by_id.get(dep_id)
        if dependency and not dependency["completed"] and not dependency["cancelled"]:
            if task_remaining_minutes(dependency) > 0:
                return True
    return False

# ============================================================
# DEADLINE / PACE CALCULATION
# ============================================================

def actual_deadline_end(task):
    deadline = task.get("deadline")
    if not deadline:
        return None
    if deadline.get("end"):
        return deadline["end"]
    return deadline["start"].replace(hour=23, minute=59, second=59, microsecond=999999)


def deadline_kind(task, today):
    deadline = actual_deadline_end(task)
    if not deadline:
        return None
    if deadline.date() < today:
        return "overdue"
    if deadline.date() == today:
        return "today"
    return "future"


def target_date_for_task(task, now):
    deadline = actual_deadline_end(task)
    if not deadline:
        return None

    deadline_date = deadline.date()
    if deadline_date <= now.date():
        return deadline_date

    preferred = deadline_date - timedelta(days=1)
    days = max(1, (preferred - now.date()).days + 1)
    required_per_day = task_remaining_minutes(task) / days
    if required_per_day > TARGET_DAILY_LIMIT_MINUTES:
        return deadline_date
    return preferred


def required_daily_minutes(task, now):
    target = target_date_for_task(task, now)
    if target is None:
        return 0
    if target < now.date():
        return task_remaining_minutes(task)
    days = max(1, (target - now.date()).days + 1)
    return task_remaining_minutes(task) / days


def days_to_target(task, now):
    target = target_date_for_task(task, now)
    if target is None:
        return None
    return max(1, (target - now.date()).days + 1)


def task_sort_key(task, now):
    remaining = task_remaining_minutes(task)
    kind = deadline_kind(task, now.date())
    required_today = required_daily_minutes(task, now)
    target = target_date_for_task(task, now)

    # Overdue items are intentionally visually and numerically obvious, but we
    # do not generate a collection of competing emergency icons.
    overdue_rank = 0 if kind == "overdue" else 1
    priority_rank = PRIORITY_RANK.get(task.get("priority"), 1)

    if target is None:
        # Deadline-free work begins around the configurable undated position.
        return (3, 0, 0, 0, task["task"].lower())

    # Tasks requiring more work today rise above tasks that can safely wait.
    # Required pace is the main signal; deadline/priority break ties.
    return (
        overdue_rank,
        0 if required_today > 0 else 1,
        -required_today,
        priority_rank,
        target.toordinal(),
        task["task"].lower(),
    )

# ============================================================
# BUMP
# ============================================================

def apply_bumps(ordered_ids, allocations, tasks_by_id, state):
    """Move each checked Bump task down three positions and clear the checkbox.

    The scheduler stores the three tasks that were skipped so the bump survives
    later rebuilds until those tasks have actually left the queue.
    """
    active = {task_id for task_id in ordered_ids}
    bump_state = state.setdefault("bumps", {})
    changed = False

    for allocation in allocations:
        if not allocation["bump"]:
            continue
        task_id = allocation_task_id(allocation, tasks_by_id)
        if not task_id or task_id not in active:
            continue
        index = ordered_ids.index(task_id)
        following = ordered_ids[index + 1:index + 1 + BUMP_DISTANCE]
        bump_state[task_id] = following
        notion("PATCH", f'pages/{allocation["page_id"]}', json={"properties": {BUMP_PROPERTY_NAME: {"checkbox": False}}})
        allocation["bump"] = False
        changed = True
        print(f'Bumped "{tasks_by_id[task_id]["task"]}" down {BUMP_DISTANCE} positions.')

    # Apply persisted bumps after the fresh urgency calculation.
    for task_id in list(bump_state):
        if task_id not in active:
            del bump_state[task_id]
            continue
        anchors = [x for x in bump_state[task_id] if x in active]
        if not anchors:
            del bump_state[task_id]
            continue
        current_index = ordered_ids.index(task_id)
        # Place after the last still-active task that the user skipped.
        desired_index = max(ordered_ids.index(x) for x in anchors) + 1
        if desired_index > current_index:
            ordered_ids.pop(current_index)
            desired_index = min(desired_index - 1, len(ordered_ids))
            ordered_ids.insert(desired_index, task_id)
        else:
            # The skipped tasks have moved below this one naturally; the bump is spent.
            del bump_state[task_id]

    return changed

# ============================================================
# ICONS
# ============================================================

def sync_icons(allocations, tasks_by_id, ordered_ids):
    order = {task_id: i for i, task_id in enumerate(ordered_ids)}
    today = datetime.now(TZ).date()
    for allocation in allocations:
        task_id = allocation_task_id(allocation, tasks_by_id)
        if not task_id:
            continue
        task = tasks_by_id[task_id]
        if allocation["completed"]:
            icon = None
        elif deadline_kind(task, today) == "overdue":
            icon = {"type": "emoji", "emoji": "❤️"}
        else:
            icon = None
        notion("PATCH", f'pages/{allocation["page_id"]}', json={"icon": icon})

# ============================================================
# ALLOCATION CREATION / UPDATE
# ============================================================

def get_allocation_schema(database_id):
    return notion("GET", f"databases/{database_id}").get("properties", {})


def ensure_allocation_properties(database_id):
    """Validate the properties the new scheduler actually needs.

    Notion's title property is allowed to have any name. We discover it from
    the schema rather than assuming it is literally called "Name".
    """
    global ALLOCATION_TITLE_PROPERTY_NAME
    schema = get_allocation_schema(database_id)

    title_properties = [
        name for name, prop in schema.items()
        if prop.get("type") == "title"
    ]
    if not title_properties:
        raise RuntimeError(
            'Task Allocations has no title property. Every Notion database '
            'must have one title property.'
        )
    ALLOCATION_TITLE_PROPERTY_NAME = title_properties[0]

    required = {
        SCHEDULE_ORDER_PROPERTY: "number",
        ALLOCATION_PROPERTY: "number",
        UNIT_PROPERTY: "select",
        COMPLETED_PROPERTY: "checkbox",
        BUMP_PROPERTY_NAME: "checkbox",
        COMPLETED_UNITS_PROPERTY_NAME: "number",
    }
    for name, expected_type in required.items():
        actual = schema.get(name)
        if not actual:
            raise RuntimeError(
                f'Task Allocations is missing required property "{name}". '
                "Add it in Notion before running the scheduler."
            )
        if actual.get("type") != expected_type:
            raise RuntimeError(
                f'Task Allocations property "{name}" is {actual.get("type")}, '
                f"but the scheduler expects {expected_type}."
            )


def create_allocation(desired):
    database_id = find_database("Task Allocations")
    data_source_id = get_data_source(database_id)
    task = desired["task"]
    unit = task["unit"]
    amount = desired["allocation_units"]
    display = format_allocation(desired["amount_minutes"], unit)
    relation_name = task["relation_name"]

    if not ALLOCATION_TITLE_PROPERTY_NAME:
        raise RuntimeError("Task Allocations title property has not been initialized.")
    properties = {
        ALLOCATION_TITLE_PROPERTY_NAME: {"title": [{"text": {"content": f'{task["task"]} — {display}'}}]},
        SCHEDULE_ORDER_PROPERTY: {"number": desired["schedule_order"]},
        ALLOCATION_PROPERTY: {"number": amount},
        UNIT_PROPERTY: {"select": {"name": unit}},
        COMPLETED_PROPERTY: {"checkbox": False},
        BUMP_PROPERTY_NAME: {"checkbox": False},
        COMPLETED_UNITS_PROPERTY_NAME: {"number": 0},
        CANCEL_PROPERTY_NAME: {"checkbox": False},
        relation_name: {"relation": [{"id": task["page_id"]}]},
    }
    result = notion(
        "POST",
        "pages",
        json={"parent": {"data_source_id": data_source_id}, "properties": properties},
    )
    time.sleep(CREATE_REQUEST_DELAY)
    return result


def update_allocation(allocation, desired):
    task = desired["task"]
    unit = task["unit"]
    display = format_allocation(desired["amount_minutes"], unit)
    if not ALLOCATION_TITLE_PROPERTY_NAME:
        raise RuntimeError("Task Allocations title property has not been initialized.")
    properties = {
        ALLOCATION_TITLE_PROPERTY_NAME: {"title": [{"text": {"content": f'{task["task"]} — {display}'}}]},
        SCHEDULE_ORDER_PROPERTY: {"number": desired["schedule_order"]},
        ALLOCATION_PROPERTY: {"number": desired["allocation_units"]},
        UNIT_PROPERTY: {"select": {"name": unit}},
    }
    notion("PATCH", f'pages/{allocation["page_id"]}', json={"properties": properties})

# ============================================================
# QUEUE BUILDING
# ============================================================

def build_queue(tasks, allocations, state):
    now = datetime.now(TZ)
    tasks_by_id, _ = build_dependency_graph(tasks)
    active = [
        task for task in tasks
        if not task["completed"] and not task["cancelled"] and task_remaining_minutes(task) > 0
    ]

    # Dependencies are still respected, but do not create a second urgency system.
    eligible = [task for task in active if not dependency_blocked(task, tasks_by_id)]
    blocked = [task for task in active if dependency_blocked(task, tasks_by_id)]
    eligible.sort(key=lambda task: task_sort_key(task, now))
    blocked.sort(key=lambda task: task_sort_key(task, now))

    # Deadline-free tasks are deliberately visible but begin later in the queue.
    dated = [t for t in eligible if t.get("deadline")]
    undated = [t for t in eligible if not t.get("deadline")]
    undated.sort(key=lambda task: (PRIORITY_RANK.get(task.get("priority"), 1), task["task"].lower()))

    ordered = dated + undated + blocked
    ordered_ids = [task["page_id"] for task in ordered]

    # Apply Bump before assigning Schedule Order.
    apply_bumps(ordered_ids, allocations, tasks_by_id, state)

    # Deadline-free work should begin around position 18 when enough dated work exists.
    dated_ids = [x for x in ordered_ids if tasks_by_id[x].get("deadline")]
    undated_ids = [x for x in ordered_ids if not tasks_by_id[x].get("deadline")]
    if undated_ids and len(dated_ids) >= UNDATED_START_ORDER:
        prefix = dated_ids[:UNDATED_START_ORDER - 1]
        suffix = dated_ids[UNDATED_START_ORDER - 1:]
        ordered_ids = prefix + undated_ids + suffix

    return ordered_ids, tasks_by_id


def allocation_units_for_task(task, now):
    """Return the amount worth doing now for the Daily Plan row.

    This is the daily pace needed to reach the target completion date. The row
    remains visible even when the allocation is small, so the user sees the
    whole task while the Allocation property tells them today's recommended
    amount.
    """
    remaining = task_remaining_minutes(task)
    if remaining <= 0:
        return 0
    daily_minutes = required_daily_minutes(task, now)
    if daily_minutes <= 0:
        daily_minutes = remaining
    daily_minutes = min(remaining, max(1, daily_minutes))
    return daily_minutes


def reconcile_allocations(tasks, allocations, ordered_ids):
    tasks_by_id = {task["page_id"]: task for task in tasks}
    existing_by_task = {}
    for allocation in allocations:
        task_id = allocation_task_id(allocation, tasks_by_id)
        if task_id:
            existing_by_task.setdefault(task_id, []).append(allocation)

    desired_ids = set(ordered_ids)
    used = set()
    now = datetime.now(TZ)

    for order, task_id in enumerate(ordered_ids, start=1):
        task = tasks_by_id[task_id]
        desired = {
            "task": task,
            "amount_minutes": allocation_units_for_task(task, now),
            "allocation_units": 0,
            "schedule_order": order,
        }
        desired["allocation_units"] = minutes_to_units(desired["amount_minutes"], task["unit"])

        reusable = next((a for a in existing_by_task.get(task_id, []) if a["page_id"] not in used), None)
        if reusable:
            update_allocation(reusable, desired)
            used.add(reusable["page_id"])
        else:
            create_allocation(desired)

    # Keep completed allocation history. Remove only unfinished allocations
    # whose source task is no longer active in the current queue.
    for allocation in allocations:
        if allocation["page_id"] in used or allocation["completed"]:
            continue
        task_id = allocation_task_id(allocation, tasks_by_id)
        if task_id and task_id not in desired_ids:
            archive_page(allocation["page_id"])

# ============================================================
# CANCELLATION
# ============================================================

def process_cancellations(tasks, allocations):
    """Cancel is an action on Daily Plan; Cancelled is the persistent master state."""
    tasks_by_id = {task["page_id"]: task for task in tasks}
    changed = 0
    for allocation in allocations:
        if not allocation.get("cancel"):
            continue
        task_id = allocation_task_id(allocation, tasks_by_id)
        if not task_id:
            continue
        task = tasks_by_id[task_id]
        if not task["cancelled"]:
            update_source_task(
                task_id,
                {CANCELLED_PROPERTY_NAME: {"checkbox": True}},
            )
            task["cancelled"] = True
        notion(
            "PATCH",
            f'pages/{allocation["page_id"]}',
            json={"properties": {CANCEL_PROPERTY_NAME: {"checkbox": False}}},
        )
        changed += 1
        print(f'Cancelled "{task["task"]}".')
    return changed


def checkbox_value_from_cached(item, key):
    return bool(item.get(key, False))

# ============================================================
# STATE / CHANGE DETECTION
# ============================================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)
    os.replace(temporary, STATE_FILE)


def task_fingerprint(tasks):
    return stable_hash([
        (
            t["page_id"],
            t["task"],
            t["database"],
            t["deadline"]["start"].isoformat() if t["deadline"] else None,
            t["deadline"]["end"].isoformat() if t["deadline"] and t["deadline"].get("end") else None,
            t["workload"],
            t["completed_units"],
            t["unit"],
            t["priority"],
            t["completed"],
            t["cancelled"],
            tuple(sorted(t["dependencies"])),
        )
        for t in sorted(tasks, key=lambda x: x["page_id"])
    ])


def allocation_fingerprint(allocations, tasks_by_id):
    rows = []
    for a in allocations:
        rows.append((
            a["page_id"],
            allocation_task_id(a, tasks_by_id),
            a["completed"],
            a["bump"],
            a["completed_units"],
            a["allocation"],
        ))
    return stable_hash(sorted(rows))

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 50)
    print("NOTION DAILY QUEUE SCHEDULER")
    print("=" * 50)

    state = load_state()

    tasks = read_tasks()
    allocations_database_id = find_database("Task Allocations")
    ensure_allocation_properties(allocations_database_id)
    allocations = read_allocations()
    tasks_by_id = {task["page_id"]: task for task in tasks}

    # Daily Plan completion is authoritative for the linked master task.
    sync_daily_completion_to_master(tasks, allocations)
    sync_partial_units_to_master(tasks, allocations, state)

    # Re-read source tasks after any completion/progress synchronization.
    tasks = read_tasks()
    tasks_by_id = {task["page_id"]: task for task in tasks}
    allocations = read_allocations()

    process_cancellations(tasks, allocations)

    # Re-read after completion/progress/cancellation actions so the queue is
    # built from the current master To-Do state.
    tasks = read_tasks()
    allocations = read_allocations()
    tasks_by_id = {task["page_id"]: task for task in tasks}

    ordered_ids, tasks_by_id = build_queue(tasks, allocations, state)

    print("\nDaily Plan order:")
    for i, task_id in enumerate(ordered_ids, start=1):
        task = tasks_by_id[task_id]
        pace = required_daily_minutes(task, datetime.now(TZ))
        print(f"  {i:>2}. {task['task']} — {format_minutes(pace)} today")

    reconcile_allocations(tasks, allocations, ordered_ids)

    # Re-read so icon updates and the saved fingerprint reflect the actual pages.
    refreshed_allocations = read_allocations()
    sync_icons(refreshed_allocations, tasks_by_id, ordered_ids)

    state["tasks"] = task_fingerprint(tasks)
    state["allocations"] = allocation_fingerprint(refreshed_allocations, tasks_by_id)
    save_state(state)

    print("\nScheduler finished successfully.")


if __name__ == "__main__":
    main()
