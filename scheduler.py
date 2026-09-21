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

# Existing Notion dependency relation maintained by the user's
# Notion automation. The scheduler reads the prerequisites from this
# property; it does not create or modify dependency relationships.
DEPENDENCY_PROPERTY_NAME = "Blocked by"

# A prerequisite should be finished before the dependent task's
# own work begins in earnest.  We therefore work backward from
# the dependent's deadline, reserving:
#   1) the dependent's remaining workload, and
#   2) one full working day of breathing room.
DEPENDENCY_BUFFER_WORKING_DAYS = 1

PFS_TASK_NAME = "PFS weekly hours"
PFS_PROJECT_NAME = "PFS"
PFS_WEEKLY_TARGET_MINUTES = 30 * 60

CREATE_REQUEST_DELAY = 0.5
MAX_RATE_LIMIT_RETRIES = 5

STATE_FILE = os.environ.get("SCHEDULER_STATE_FILE", ".scheduler_state.json")


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
        raise RuntimeError(f'Unknown workload unit "{unit}".')
    return workload * MINUTES_PER_UNIT[unit]


def minutes_to_units(minutes, unit):
    if unit not in MINUTES_PER_UNIT:
        raise RuntimeError(f'Unknown workload unit "{unit}".')
    return minutes / MINUTES_PER_UNIT[unit]


def format_minutes(minutes):
    minutes = int(round(minutes))
    hours = minutes // 60
    remainder = minutes % 60
    parts = []

    if hours:
        parts.append("1 hour" if hours == 1 else f"{hours} hours")
    if remainder:
        parts.append("1 minute" if remainder == 1 else f"{remainder} minutes")

    return " ".join(parts) if parts else "0 minutes"


def format_allocation(amount_minutes, unit):
    if unit in ("Hours", "Minutes"):
        return format_minutes(amount_minutes)

    amount = minutes_to_units(amount_minutes, unit)
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
                    f"Notion API rate limit persisted after "
                    f"{MAX_RATE_LIMIT_RETRIES} retries: {response.text}"
                )

            print(
                f"  Notion rate limit reached. "
                f"Waiting {retry_after} seconds..."
            )
            time.sleep(max(1, retry_after))
            continue

        raise RuntimeError(
            f"Notion API error {response.status_code}: {response.text}"
        )

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
            break
        cursor = data.get("next_cursor")

    return results


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
            database_id = obj.get("parent", {}).get("database_id")
            if not database_id:
                continue

            database = notion("GET", f"databases/{database_id}")
            title = "".join(
                item.get("plain_text", "")
                for item in database.get("title", [])
            ).strip()

            if title == name:
                return database_id

    raise RuntimeError(f'Could not find Notion database "{name}".')


def get_data_source(database_id):
    data = notion("GET", f"databases/{database_id}")
    sources = data.get("data_sources", [])

    if not sources:
        raise RuntimeError(
            f"No data source found for database {database_id}."
        )

    return sources[0]["id"]


def query_data_source(data_source_id):
    pages = []
    cursor = None

    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor

        data = notion(
            "POST",
            f"data_sources/{data_source_id}/query",
            json=payload,
        )
        pages.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return pages


def archive_page(page_id):
    notion("PATCH", f"pages/{page_id}", json={"archived": True})


# ============================================================
# PROPERTY HELPERS
# ============================================================

def title_value(page, property_name):
    prop = page["properties"].get(property_name)
    if not prop or prop["type"] != "title":
        return ""

    return "".join(
        item.get("plain_text", "")
        for item in prop.get("title", [])
    )


def checkbox_value(page, property_name):
    prop = page["properties"].get(property_name)
    if not prop or prop["type"] != "checkbox":
        return False
    return prop.get("checkbox", False)


def number_value(page, property_name):
    prop = page["properties"].get(property_name)
    if not prop or prop["type"] != "number":
        return None
    return prop.get("number")


def select_value(page, property_name):
    prop = page["properties"].get(property_name)
    if not prop or prop["type"] != "select":
        return None

    option = prop.get("select")
    return option.get("name") if option else None


def date_value(page, property_name):
    prop = page["properties"].get(property_name)
    if not prop or prop["type"] != "date":
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
        "end": parse_datetime(end) if end else None,
    }


def relation_ids(page, property_name):
    prop = page["properties"].get(property_name)
    if not prop or prop["type"] != "relation":
        return []

    return [item["id"] for item in prop.get("relation", [])]


def find_dependency_property_name(page):
    """Return the authoritative dependency relation property name.

    The user's Notion automation maintains the native dependency
    relationship through the "Blocked by" relation. The GitHub
    scheduler only needs the upstream/prerequisite side of that
    relationship, so it reads "Blocked by" and does not maintain a
    second dependency relation of its own.
    """
    properties = page.get("properties", {})
    prop = properties.get(DEPENDENCY_PROPERTY_NAME)

    if prop and prop.get("type") == "relation":
        return DEPENDENCY_PROPERTY_NAME

    return None


def parse_datetime(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ)


# ============================================================
# SOURCE TASK DATABASES
# ============================================================

def relation_target_databases(database_id):
    """Return Task Allocations relation properties that point to task databases."""
    database = notion("GET", f"databases/{database_id}")
    targets = []

    for name, prop in database.get("properties", {}).items():
        if prop.get("type") != "relation":
            continue

        if name == "Focus time":
            continue

        target_id = prop.get("relation", {}).get("database_id")
        if target_id:
            targets.append((name, target_id))

    return targets


def first_property_name(page, names, property_type=None):
    properties = page.get("properties", {})

    for name in names:
        prop = properties.get(name)
        if prop and (property_type is None or prop.get("type") == property_type):
            return name

    if property_type:
        for name, prop in properties.items():
            if prop.get("type") == property_type:
                return name

    return None


def read_source_tasks():
    """Read tasks directly from the small task databases.

    Task Allocations contains one relation for each source task database.
    Those relations tell the scheduler which databases to read, so there
    is no master task database.
    """
    allocations_database_id = find_database("Task Allocations")
    source_relations = relation_target_databases(allocations_database_id)

    if not source_relations:
        raise RuntimeError(
            'No task-database relations were found in "Task Allocations".'
        )

    tasks = []

    for relation_name, source_database_id in source_relations:
        data_source_id = get_data_source(source_database_id)
        pages = query_data_source(data_source_id)

        for page in pages:
            title_property = first_property_name(page, ["Name", "Task"], "title")
            name = title_value(page, title_property).strip() if title_property else ""
            if not name:
                continue

            workload = number_value(page, "Workload")
            unit = select_value(page, "Unit")

            # Empty/new pages are ignored rather than becoming zero-minute
            # scheduler tasks.
            if workload is None or not unit:
                continue

            tasks.append({
                "page_id": page["id"],
                "task": name,
                "project": select_value(page, "Project"),
                "deadline": date_value(page, "Deadline"),
                "workload": workload,
                "unit": unit,
                "priority": select_value(page, "Priority") or select_value(page, "Priority level"),
                "continuous": checkbox_value(page, "Continuous"),
                "completed": checkbox_value(page, "Completed"),
                "minutes": workload_to_minutes(workload, unit),
                "dependencies": relation_ids(page, DEPENDENCY_PROPERTY_NAME),
                "allocation_relation_property": relation_name,
                "source_database_id": source_database_id,
            })

        print(f'  {relation_name}: {len(pages)} pages read')

    print(f"Source task databases found: {len(source_relations)}")
    print(f"Source tasks found: {len(tasks)}")
    return tasks


# ============================================================
# FOCUS TIME
# ============================================================

def read_focus_time():
    database_id = find_database("Focus time")
    data_source_id = get_data_source(database_id)
    pages = query_data_source(data_source_id)

    now = datetime.now(TZ)
    horizon = now + timedelta(days=PLANNING_DAYS)

    all_blocks = []

    for page in pages:
        date = date_value(page, "Date")
        if not date or not date["end"]:
            continue

        all_blocks.append({
            "page_id": page["id"],
            "start": date["start"],
            "end": date["end"],
            "last_edited_time": page.get("last_edited_time"),
        })

    all_blocks.sort(key=lambda block: block["start"])

    schedulable_blocks = []

    for block in all_blocks:
        start = block["start"]
        end = block["end"]

        if end <= now:
            continue
        if start >= horizon:
            continue

        if start < now:
            start = now
        if end > horizon:
            end = horizon

        minutes = int((end - start).total_seconds() / 60)
        if minutes <= 0:
            continue

        schedulable_blocks.append({
            "page_id": block["page_id"],
            "start": start,
            "end": end,
            "capacity": minutes,
            "remaining": minutes,
        })

    return all_blocks, schedulable_blocks


# ============================================================
# TASK ALLOCATIONS
# ============================================================

def all_relation_ids(page):
    """Return task IDs from every task relation on a Task Allocation page."""
    ids = []
    for name, prop in page.get("properties", {}).items():
        if prop.get("type") != "relation":
            continue
        if name == "Focus time":
            continue
        ids.extend(item["id"] for item in prop.get("relation", []))
    return ids


def read_allocations():
    database_id = find_database("Task Allocations")
    data_source_id = get_data_source(database_id)
    pages = query_data_source(data_source_id)

    allocations = []

    for page in pages:
        allocations.append({
            "page_id": page["id"],
            "name": title_value(page, "Name"),
            "focus_ids": relation_ids(page, "Focus time"),
            "task_ids": all_relation_ids(page),
            "allocation": number_value(page, "Allocation") or 0,
            "completed": checkbox_value(page, "Completion"),
        })

    return allocations


# ============================================================
# ALLOCATION ACCOUNTING
# ============================================================

def allocation_minutes(allocation, tasks_by_id):
    total = 0

    for task_id in allocation["task_ids"]:
        task = tasks_by_id.get(task_id)
        if not task:
            continue

        total += workload_to_minutes(
            allocation["allocation"],
            task["unit"],
        )

    return total


def calculate_completed_work(allocations, tasks_by_id):
    completed = {}

    for allocation in allocations:
        if not allocation["completed"]:
            continue

        minutes = allocation_minutes(
            allocation,
            tasks_by_id,
        )

        for task_id in allocation["task_ids"]:
            completed[task_id] = (
                completed.get(task_id, 0) + minutes
            )

    return completed


def current_week_range():
    today = datetime.now(TZ).date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=7)


def pfs_minutes_this_week(
    allocations,
    all_focus_blocks,
    tasks_by_id,
):
    week_start, week_end = current_week_range()

    blocks_by_id = {
        block["page_id"]: block
        for block in all_focus_blocks
    }

    total = 0

    for allocation in allocations:
        is_pfs = False
        pfs_unit = None

        for task_id in allocation["task_ids"]:
            task = tasks_by_id.get(task_id)
            if not task:
                continue

            # The synthetic weekly-hours task is a scheduling target,
            # not actual PFS work. It must never count toward itself.
            if task["task"] == PFS_TASK_NAME:
                continue

            if task["project"] == PFS_PROJECT_NAME:
                is_pfs = True
                pfs_unit = task["unit"]
                break

        if not is_pfs:
            continue

        if pfs_unit not in MINUTES_PER_UNIT:
            continue

        for focus_id in allocation["focus_ids"]:
            block = blocks_by_id.get(focus_id)
            if not block:
                continue

            date = block["start"].date()
            if week_start <= date < week_end:
                total += workload_to_minutes(
                    allocation["allocation"],
                    pfs_unit,
                )

    return total


# ============================================================
# DEPENDENCIES
# ============================================================

def build_dependency_graph(tasks):
    tasks_by_id = {task["page_id"]: task for task in tasks}
    dependents = {task["page_id"]: set() for task in tasks}

    for task in tasks:
        valid_dependencies = set()
        for dependency_id in task["dependencies"]:
            # A task cannot meaningfully depend on itself.
            # Ignore an accidental self-link rather than stopping the
            # entire scheduler.
            if dependency_id == task["page_id"]:
                print(
                    f'Warning: self-dependency ignored for "{task["task"]}".'
                )
                continue

            if dependency_id not in tasks_by_id:
                print(
                    f'Warning: dependency on an unavailable page was '
                    f'ignored for "{task["task"]}".'
                )
                continue

            valid_dependencies.add(dependency_id)
            dependents[dependency_id].add(task["page_id"])

        task["dependencies"] = sorted(valid_dependencies)

    # Detect cycles before scheduling.
    visiting = set()
    visited = set()

    def visit(task_id, path):
        if task_id in visiting:
            cycle_start = path.index(task_id)
            cycle_ids = path[cycle_start:] + [task_id]
            cycle_names = [tasks_by_id[i]["task"] for i in cycle_ids]
            raise RuntimeError(
                "Circular dependency detected: "
                + " -> ".join(cycle_names)
            )

        if task_id in visited:
            return

        visiting.add(task_id)
        for dependency_id in tasks_by_id[task_id]["dependencies"]:
            visit(dependency_id, path + [dependency_id])
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task["page_id"], [task["page_id"]])

    return tasks_by_id, dependents


def subtract_working_days(dt, days):
    result = dt
    remaining_days = days

    while remaining_days > 0:
        result -= timedelta(days=1)
        if result.weekday() < 5:
            remaining_days -= 1

    return result


def dependency_buffer_for_task(task):
    return timedelta(
        days=DEPENDENCY_BUFFER_WORKING_DAYS
    )


def calculate_dependency_deadlines(tasks, remaining):
    """
    Work backward through dependency chains.

    For a prerequisite A -> B, B's effective deadline becomes the
    latest point at which B can still be comfortably completed. A's
    effective deadline is then:

        B effective deadline
        - B remaining workload
        - one full working day of buffer

    The earliest constraint wins when a task has both its own deadline
    and a downstream dependency deadline.
    """
    tasks_by_id, dependents = build_dependency_graph(tasks)

    effective = {
        task_id: task["deadline"]["start"]
        if task["deadline"]
        else None
        for task_id, task in tasks_by_id.items()
    }

    visiting = set()

    def solve(task_id):
        if task_id in visiting:
            raise RuntimeError("Circular dependency detected while calculating deadlines.")

        visiting.add(task_id)

        # First make sure all downstream constraints are known.
        for dependent_id in dependents[task_id]:
            solve(dependent_id)

            dependent = tasks_by_id[dependent_id]
            dependent_remaining = remaining.get(dependent_id, 0)

            # A fully completed downstream task creates no future
            # scheduling pressure.
            if dependent["completed"] or dependent_remaining <= 0:
                continue

            downstream_deadline = effective.get(dependent_id)
            if downstream_deadline is None:
                continue

            candidate = (
                downstream_deadline
                - timedelta(minutes=dependent_remaining)
                - dependency_buffer_for_task(dependent)
            )

            own_deadline = effective.get(task_id)
            if own_deadline is None or candidate < own_deadline:
                effective[task_id] = candidate

        visiting.remove(task_id)
        return effective.get(task_id)

    for task in tasks:
        solve(task["page_id"])

    for task in tasks:
        task["effective_deadline"] = effective.get(task["page_id"])

    return tasks_by_id, dependents


def dependency_blocked(task, tasks_by_id, completed, remaining):
    if not task["dependencies"]:
        return False

    for dependency_id in task["dependencies"]:
        dependency = tasks_by_id.get(dependency_id)
        if not dependency:
            continue

        # A dependency is satisfied only by ACTUAL completed work,
        # not merely because the prerequisite has been allocated in the
        # new schedule. This prevents a dependent task from being placed
        # in the same rebuild before its prerequisite is actually done.
        if dependency["completed"]:
            continue

        completed_minutes = completed.get(dependency_id, 0)
        if completed_minutes >= dependency["minutes"]:
            continue

        return True

    return False


def dependency_depth(task_id, tasks_by_id, cache=None):
    if cache is None:
        cache = {}

    if task_id in cache:
        return cache[task_id]

    task = tasks_by_id[task_id]
    if not task["dependencies"]:
        cache[task_id] = 0
        return 0

    depth = 1 + max(
        dependency_depth(dep_id, tasks_by_id, cache)
        for dep_id in task["dependencies"]
        if dep_id in tasks_by_id
    )
    cache[task_id] = depth
    return depth


# ============================================================
# CHANGE DETECTION
# ============================================================

def stable_hash(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_focus_fingerprint(all_focus_blocks):
    horizon = datetime.now(TZ) + timedelta(days=PLANNING_DAYS)
    return stable_hash([
        (
            block["page_id"],
            block["start"].isoformat(),
            block["end"].isoformat(),
            block.get("last_edited_time"),
        )
        for block in all_focus_blocks
        if block["start"] < horizon
    ])


def build_task_fingerprint(tasks):
    return stable_hash([
        (
            task["page_id"],
            task["task"],
            task["project"],
            task["deadline"]["start"].isoformat()
            if task["deadline"] else None,
            task["deadline"]["end"].isoformat()
            if task["deadline"] and task["deadline"]["end"] else None,
            task["workload"],
            task["unit"],
            task["priority"],
            task["continuous"],
            task["completed"],
            tuple(sorted(task["dependencies"])),
        )
        for task in sorted(tasks, key=lambda t: t["page_id"])
    ])


def build_completed_allocation_fingerprint(allocations):
    return stable_hash(sorted(
        (
            allocation["page_id"],
            tuple(sorted(allocation["task_ids"])),
            allocation["completed"],
            allocation["allocation"],
        )
        for allocation in allocations
        if allocation["completed"]
    ))


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print("Warning: scheduler state file is invalid; rebuilding.")
        return {}


def save_state(state):
    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)

    os.replace(temp_file, STATE_FILE)


# ============================================================
# ACTIVE FOCUS TIME SAFETY
# ============================================================

def active_focus_block(all_focus_blocks, now):
    for block in all_focus_blocks:
        if block["start"] <= now < block["end"]:
            return block
    return None


def should_defer_rebuild(all_focus_blocks):
    now = datetime.now(TZ)
    block = active_focus_block(all_focus_blocks, now)

    if block:
        print(
            "A Focus Time block is currently active "
            f"({block['start'].strftime('%I:%M %p').lstrip('0')}–"
            f"{block['end'].strftime('%I:%M %p').lstrip('0')})."
        )
        print("Rebuild deferred until that block has ended.")
        return True

    return False


# ============================================================
# SCHEDULING RULES
# ============================================================

def priority_multiplier(priority):
    if priority == "High":
        return 1.20
    if priority == "Medium":
        return 1.05
    return 1.00


def hours_until_effective_deadline(task, now):
    deadline = task.get("effective_deadline")
    if not deadline:
        return None

    return (deadline - now).total_seconds() / 3600


def task_score(task, remaining_minutes, now, same_day_minutes, dependency_depth_value):
    hours = hours_until_effective_deadline(task, now)

    if hours is None:
        deadline_score = 0.1
    elif hours <= 0:
        deadline_score = 100000000
    else:
        deadline_score = 100 / ((hours + 1) ** 2)

    score = (
        deadline_score
        * priority_multiplier(task["priority"])
        + min(2.0, remaining_minutes / 120)
    )

    # Dependencies create additional urgency, but the main driver is
    # still the effective deadline calculated from downstream work.
    if dependency_depth_value > 0:
        score *= 1.0 + min(0.20, 0.05 * dependency_depth_value)

    if same_day_minutes > 0:
        score *= 0.85

    return score


def task_is_eligible(
    task,
    remaining_minutes,
    block_start,
    block_end,
    now,
    tasks_by_id,
    completed,
    remaining,
):
    if remaining_minutes <= 0:
        return False

    if task["completed"]:
        return False

    # A dependent task cannot begin until all prerequisites are done.
    if dependency_blocked(task, tasks_by_id, completed, remaining):
        return False

    effective_deadline = task.get("effective_deadline")

    # No actual or inherited deadline: eligible normally.
    if not effective_deadline:
        return True

    # If the effective deadline has passed, the task becomes urgent.
    if effective_deadline <= now:
        return True

    # Preserve the existing deadline-day rule for the task's actual
    # deadline. Dependency-derived deadlines are planning targets, so
    # they remain schedulable right up to their barrier.
    actual_deadline = task["deadline"]["start"] if task["deadline"] else None
    if actual_deadline and actual_deadline.date() == now.date():
        if remaining_minutes > MIN_CHUNK_MINUTES:
            return False

    # Never schedule work into or beyond the effective planning
    # deadline unless that deadline has already become urgent.
    if block_start >= effective_deadline:
        return False

    # Also preserve the actual hard deadline.
    if actual_deadline and block_start >= actual_deadline:
        return False

    return True


def choose_allocation_size(
    task,
    remaining_minutes,
    available_minutes,
    block_start,
    block_end,
    now,
):
    maximum = min(remaining_minutes, available_minutes)

    # Hard barrier for both the task's own deadline and its
    # dependency-derived planning deadline.
    barriers = []
    if task.get("effective_deadline") and task["effective_deadline"] > now:
        barriers.append(task["effective_deadline"])
    if task["deadline"] and task["deadline"]["start"] > now:
        barriers.append(task["deadline"]["start"])

    if barriers:
        earliest_barrier = min(barriers)
        deadline_capacity = int(
            (earliest_barrier - block_start).total_seconds() / 60
        )
        maximum = min(maximum, deadline_capacity)

    if maximum <= 0:
        return 0

    if task["continuous"]:
        if remaining_minutes <= maximum:
            return remaining_minutes
        return 0

    if maximum <= PREFERRED_MAX_CHUNK_MINUTES:
        return maximum

    chunk = PREFERRED_MAX_CHUNK_MINUTES
    remainder = remaining_minutes - chunk

    if 0 < remainder <= MIN_CHUNK_MINUTES:
        return remaining_minutes

    return chunk


# ============================================================
# PFS
# ============================================================

def pfs_score(block, remaining_target, week_end):
    block_date = block["start"].date()
    is_weekday = block_date.weekday() < 5

    days_left = max(1, (week_end - block_date).days)

    score = 0.35 if is_weekday else 0.20
    score += min(2.0, 7.0 / days_left)

    deficit_hours = remaining_target / 60
    score += min(2.5, deficit_hours / 12)

    return score


# ============================================================
# GENERAL + PFS COMPETITIVE SCHEDULING
# ============================================================

def schedule_tasks(
    tasks,
    focus_blocks,
    completed,
    pfs_task,
    completed_pfs_minutes,
):
    now = datetime.now(TZ)

    remaining = {}
    for task in tasks:
        if task["completed"]:
            continue
        if task["task"] == PFS_TASK_NAME:
            continue

        task_id = task["page_id"]
        remaining[task_id] = max(
            0,
            task["minutes"] - completed.get(task_id, 0),
        )

    tasks_by_id, _ = build_dependency_graph(tasks)
    dependency_depth_cache = {}

    calculate_dependency_deadlines(tasks, remaining)

    for task in tasks:
        depth = dependency_depth(task["page_id"], tasks_by_id, dependency_depth_cache)
        task["dependency_depth"] = depth

    pfs_remaining_target = max(
        0,
        PFS_WEEKLY_TARGET_MINUTES - completed_pfs_minutes,
    )

    new_allocations = []

    for block in focus_blocks:
        excluded_for_block = set()

        while block["remaining"] >= MIN_CHUNK_MINUTES:
            candidates = []

            for task in tasks:
                if task["task"] == PFS_TASK_NAME:
                    continue
                if task["completed"]:
                    continue

                task_id = task["page_id"]
                if task_id in excluded_for_block:
                    continue
                rem = remaining.get(task_id, 0)
                if rem <= 0:
                    continue

                if not task_is_eligible(
                    task,
                    rem,
                    block["start"],
                    block["end"],
                    now,
                    tasks_by_id,
                    completed,
                    remaining,
                ):
                    continue

                same_day_minutes = 0
                for allocation in new_allocations:
                    if allocation["task"]["page_id"] != task_id:
                        continue

                    allocation_block = next(
                        (
                            candidate_block
                            for candidate_block in focus_blocks
                            if candidate_block["page_id"]
                            == allocation["focus_page_id"]
                        ),
                        None,
                    )

                    if (
                        allocation_block
                        and allocation_block["start"].date()
                        == block["start"].date()
                    ):
                        same_day_minutes += allocation["amount_minutes"]

                score = task_score(
                    task,
                    rem,
                    now,
                    same_day_minutes,
                    task.get("dependency_depth", 0),
                )

                candidates.append(("general", score, task))

            # PFS is deliberately only a slight preference. It competes
            # with ordinary work and yields to genuinely urgent work.
            if (
                pfs_task
                and pfs_remaining_target >= MIN_CHUNK_MINUTES
                and current_week_range()[0]
                <= block["start"].date()
                < current_week_range()[1]
            ):
                candidates.append((
                    "pfs",
                    pfs_score(
                        block,
                        pfs_remaining_target,
                        current_week_range()[1],
                    ),
                    pfs_task,
                ))

            if not candidates:
                break

            overdue = [
                candidate
                for candidate in candidates
                if (
                    candidate[0] == "general"
                    and candidate[2].get("effective_deadline")
                    and candidate[2]["effective_deadline"] <= now
                )
            ]

            if overdue:
                candidates = overdue

            candidates.sort(key=lambda item: item[1], reverse=True)
            kind, _, chosen = candidates[0]

            if kind == "pfs":
                amount = min(
                    block["remaining"],
                    pfs_remaining_target,
                    PREFERRED_MAX_CHUNK_MINUTES,
                )

                if amount < MIN_CHUNK_MINUTES:
                    break

                new_allocations.append({
                    "task": chosen,
                    "amount_minutes": amount,
                    "focus_page_id": block["page_id"],
                })

                block["remaining"] -= amount
                pfs_remaining_target -= amount
                continue

            amount = choose_allocation_size(
                chosen,
                remaining[chosen["page_id"]],
                block["remaining"],
                block["start"],
                block["end"],
                now,
            )

            if amount <= 0:
                # The task cannot fit in this block before its current
                # barrier. Temporarily exclude it from this block and
                # let another eligible task use the available time.
                excluded_for_block.add(chosen["page_id"])
                continue

            new_allocations.append({
                "task": chosen,
                "amount_minutes": amount,
                "focus_page_id": block["page_id"],
            })

            block["remaining"] -= amount
            remaining[chosen["page_id"]] -= amount

    return new_allocations, remaining


# ============================================================
# CREATE TASK ALLOCATION
# ============================================================

def create_allocation(allocation):
    database_id = find_database("Task Allocations")
    data_source_id = get_data_source(database_id)

    task = allocation["task"]
    amount_minutes = allocation["amount_minutes"]

    unit = task["unit"]
    if task["task"] == PFS_TASK_NAME:
        unit = "Hours"

    display_amount = format_allocation(amount_minutes, unit)
    name = f'{task["task"]} — {display_amount}'
    if allocation.get("is_final"):
        name += " (final)"

    properties = {
        "Name": {
            "title": [{
                "text": {"content": name}
            }]
        },
        "Focus time": {
            "relation": [{
                "id": allocation["focus_page_id"]
            }]
        },
        "Allocation": {
            "number": minutes_to_units(
                amount_minutes,
                unit,
            )
        },
        "Unit": {
            "select": {"name": unit}
        },
        "Completion": {
            "checkbox": False
        },
        task["allocation_relation_property"]: {
            "relation": [{
                "id": task["page_id"]
            }]
        },
    }

    result = notion(
        "POST",
        "pages",
        json={
            "parent": {
                "data_source_id": data_source_id
            },
            "properties": properties,
        },
    )

    time.sleep(CREATE_REQUEST_DELAY)
    return result


# ============================================================
# STATUS
# ============================================================

def calculate_status(tasks, completed, focus_blocks, completed_pfs_minutes=0):
    """Calculate schedule sufficiency before scheduling consumes capacity."""
    now = datetime.now(TZ)

    available = sum(block["remaining"] for block in focus_blocks)

    deadline_required = 0
    total_remaining = 0

    for task in tasks:
        if task["completed"] or task["task"] == PFS_TASK_NAME:
            continue

        rem = max(0, task["minutes"] - completed.get(task["page_id"], 0))
        if rem <= 0:
            continue

        total_remaining += rem

        if task["deadline"]:
            deadline = task["deadline"]["start"]
            if deadline <= now + timedelta(days=18):
                deadline_required += rem

    pfs_required = 0
    pfs_task_active = any(
        task["task"] == PFS_TASK_NAME and not task["completed"]
        for task in tasks
    )
    if pfs_task_active:
        pfs_required = max(
            0,
            PFS_WEEKLY_TARGET_MINUTES - completed_pfs_minutes,
        )

    required = deadline_required + pfs_required
    difference = available - required

    if difference >= 0:
        status = "🟢 On track"
        status_detail = (
            "🟢 On track — enough time available "
            f"({format_minutes(difference)} surplus)"
        )
    else:
        status = "🟠 Needs attention"
        status_detail = (
            "🟠 Needs attention — "
            f"{format_minutes(abs(difference))} "
            "additional Focus Time needed"
        )

    print(
        f"Schedule capacity: {format_minutes(available)} Focus Time available"
    )
    print(
        f"Near-term deadline work: {format_minutes(deadline_required)} required"
    )
    if pfs_task_active:
        print(
            f"PFS weekly target remaining: {format_minutes(pfs_required)}"
        )
    print(
        f"Total remaining task work: {format_minutes(total_remaining)}"
    )

    return {
        "status": status,
        "status_detail": status_detail,
        "available": available,
        "deadline_required": deadline_required,
        "pfs_required": pfs_required,
        "total_remaining": total_remaining,
        "difference": difference,
        "pfs_active": pfs_task_active,
    }


def calculate_completed_pfs_minutes(allocations, tasks_by_id):
    """Return completed PFS project time for the current week."""
    completed_pfs_minutes = 0

    for allocation in allocations:
        if not allocation["completed"]:
            continue

        for task_id in allocation["task_ids"]:
            task = tasks_by_id.get(task_id)
            if not task or task["task"] == PFS_TASK_NAME:
                continue

            if task["project"] == PFS_PROJECT_NAME:
                completed_pfs_minutes += workload_to_minutes(
                    allocation["allocation"],
                    task["unit"],
                )
                break

    return completed_pfs_minutes


def update_schedule_status(tasks, allocations, focus_blocks):
    """Update the single Current Schedule page and read its controls."""
    tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    completed = calculate_completed_work(
        allocations,
        tasks_by_id,
    )
    completed_pfs_minutes = calculate_completed_pfs_minutes(
        allocations,
        tasks_by_id,
    )

    status_info = calculate_status(
        tasks,
        completed,
        focus_blocks,
        completed_pfs_minutes,
    )

    database_id = find_database("Schedule Status")
    data_source_id = get_data_source(database_id)
    pages = query_data_source(data_source_id)

    current_schedule_page = None
    for page in pages:
        name = title_value(page, "Name").strip()
        if name == "Current Schedule":
            current_schedule_page = page
            break

    if not current_schedule_page:
        raise RuntimeError(
            'Could not find the "Current Schedule" page in the '
            '"Schedule Status" database.'
        )

    now = datetime.now(TZ)

    # Resolve property names from the actual page returned by Notion.
    # This keeps the scheduler tolerant of capitalization/spelling
    # differences while still failing clearly if a required property is
    # genuinely missing.
    actual_property_names = set(
        current_schedule_page.get("properties", {}).keys()
    )

    def resolve_status_property(expected_name):
        if expected_name in actual_property_names:
            return expected_name

        def normalize(name):
            return "".join(
                character.lower()
                for character in name
                if character.isalnum()
            )

        expected_normalized = normalize(expected_name)

        for actual_name in actual_property_names:
            if normalize(actual_name) == expected_normalized:
                return actual_name

        for actual_name in actual_property_names:
            actual_normalized = normalize(actual_name)
            if actual_normalized.startswith(expected_normalized):
                return actual_name

        available = ", ".join(sorted(actual_property_names))
        raise RuntimeError(
            f'Could not find Schedule Status property "{expected_name}". '
            f'Available properties are: {available}'
        )

    status_property = resolve_status_property("Status")
    deadline_property = resolve_status_property("Near-Term Deadline Work")
    pfs_property = resolve_status_property("PFS Weekly Target Remaining")
    capacity_property = resolve_status_property("Schedule Capacity")
    difference_property = resolve_status_property("Schedule difference")
    updated_property = resolve_status_property("Last Updated")
    reconsider_property = resolve_status_property("Reconsider")

    reconsider_requested = checkbox_value(
        current_schedule_page,
        reconsider_property,
    )

    properties = {
        status_property: {
            "select": {"name": status_info["status"]}
        },
        deadline_property: {
            "rich_text": [{
                "type": "text",
                "text": {
                    "content": format_minutes(
                        status_info["deadline_required"]
                    )
                },
            }]
        },
        pfs_property: {
            "rich_text": [{
                "type": "text",
                "text": {
                    "content": (
                        format_minutes(status_info["pfs_required"])
                        if status_info["pfs_active"]
                        else "Inactive"
                    )
                },
            }]
        },
        capacity_property: {
            "rich_text": [{
                "type": "text",
                "text": {
                    "content": format_minutes(status_info["available"])
                },
            }]
        },
        difference_property: {
            "rich_text": [{
                "type": "text",
                "text": {
                    "content": (
                        f"+{format_minutes(status_info['difference'])} surplus"
                        if status_info["difference"] >= 0
                        else f"-{format_minutes(abs(status_info['difference']))} needed"
                    )
                },
            }]
        },
        updated_property: {
            "date": {
                "start": now.isoformat(),
            }
        },
    }

    notion(
        "PATCH",
        f"pages/{current_schedule_page['id']}",
        json={"properties": properties},
    )

    if reconsider_requested:
        print("Reconsider request detected on Current Schedule.")

    print("Schedule Status updated in Notion.")
    print(f"  Status: {status_info['status_detail']}")
    print(
        "  Schedule difference: "
        f"{format_minutes(abs(status_info['difference']))} "
        f"{'surplus' if status_info['difference'] >= 0 else 'needed'}"
    )

    return {
        "status_info": status_info,
        "page_id": current_schedule_page["id"],
        "reconsider_requested": reconsider_requested,
        "reconsider_property": reconsider_property,
    }


def clear_reconsider_request(page_id, property_name):
    """Clear Reconsider only after a requested rebuild succeeds."""
    notion(
        "PATCH",
        f"pages/{page_id}",
        json={
            "properties": {
                property_name: {
                    "checkbox": False,
                }
            }
        },
    )
    print("Reconsider request completed; Reconsider reset to unchecked.")


# ============================================================
# REBUILD
# ============================================================

def delete_incomplete_allocations(allocations):
    incomplete = [
        allocation
        for allocation in allocations
        if not allocation["completed"]
    ]

    if not incomplete:
        print("No incomplete allocations to remove.")
        return

    print(
        f"Removing {len(incomplete)} incomplete "
        "provisional allocations..."
    )

    for allocation in incomplete:
        archive_page(allocation["page_id"])


def rebuild(tasks, all_focus_blocks, focus_blocks, allocations):
    print()
    print("Rebuilding future schedule...")

    tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    completed = calculate_completed_work(
        allocations,
        tasks_by_id,
    )

    # Validate the dependency graph before deleting provisional work.
    build_dependency_graph(tasks)

    # Completed PFS work is immutable history and counts toward the
    # weekly target. The synthetic PFS weekly-hours task does not count.
    completed_pfs_minutes = calculate_completed_pfs_minutes(
        allocations,
        tasks_by_id,
    )

    delete_incomplete_allocations(allocations)

    pfs_task = next(
        (
            task for task in tasks
            if task["task"] == PFS_TASK_NAME
            and not task["completed"]
        ),
        None,
    )

    if pfs_task:
        print("PFS weekly target: ACTIVE")
        print(
            "Completed PFS time this week: "
            f"{format_minutes(completed_pfs_minutes)}"
        )
        print(
            "PFS time still needed: "
            f"{format_minutes(max(0, PFS_WEEKLY_TARGET_MINUTES - completed_pfs_minutes))}"
        )
    else:
        print("PFS weekly target: INACTIVE")

    # Calculate schedule sufficiency BEFORE scheduling consumes block capacity.
    status_info = calculate_status(
        tasks,
        completed,
        focus_blocks,
        completed_pfs_minutes,
    )

    print()
    print("----------------------------------------")
    print(status_info["status_detail"])
    print("----------------------------------------")

    new_allocations, remaining = schedule_tasks(
        tasks,
        focus_blocks,
        completed,
        pfs_task,
        completed_pfs_minutes,
    )

    # Add "(final)" only to the last allocation for tasks that have
    # more than one allocation overall. Completed allocations count as
    # history; incomplete allocations were just removed and replaced.
    completed_counts = {}
    for allocation in allocations:
        if not allocation["completed"]:
            continue
        for task_id in allocation["task_ids"]:
            completed_counts[task_id] = completed_counts.get(task_id, 0) + 1

    new_counts = {}
    for allocation in new_allocations:
        task_id = allocation["task"]["page_id"]
        new_counts[task_id] = new_counts.get(task_id, 0) + 1

    seen_new = {}
    for allocation in new_allocations:
        task_id = allocation["task"]["page_id"]
        seen_new[task_id] = seen_new.get(task_id, 0) + 1
        total_count = completed_counts.get(task_id, 0) + new_counts.get(task_id, 0)
        allocation["is_final"] = (
            total_count > 1 and seen_new[task_id] == new_counts[task_id]
        )

    print(
        f"New allocations to create: {len(new_allocations)}"
    )

    for allocation in new_allocations:
        task = allocation["task"]
        unit = "Hours" if task["task"] == PFS_TASK_NAME else task["unit"]
        display_amount = format_allocation(
            allocation["amount_minutes"],
            unit,
        )

        print(
            f'  {task["task"]} → {display_amount}'
        )

        create_allocation(allocation)

    print()

    return {
        "completed_pfs_minutes": completed_pfs_minutes,
        "new_allocations": len(new_allocations),
    }


# ============================================================
# MAIN
# ============================================================

def main():
    print("========================================")
    print("          NOTION SCHEDULER")
    print("========================================")
    print()

    tasks = read_source_tasks()
    all_focus_blocks, focus_blocks = read_focus_time()
    allocations = read_allocations()

    print(f"Source tasks found: {len(tasks)}")
    print(f"Focus Time blocks found: {len(focus_blocks)}")
    print(f"Task Allocations found: {len(allocations)}")

    # The Schedule Status page is a separate Notion database from the
    # Daily Plan. Update it every run so the displayed capacity stays
    # current even when no schedule rebuild is needed. The page also
    # contains the explicit Reconsider control.
    schedule_status = update_schedule_status(
        tasks,
        allocations,
        focus_blocks,
    )
    reconsider_requested = schedule_status["reconsider_requested"]

    state = load_state()

    focus_fingerprint = build_focus_fingerprint(all_focus_blocks)
    task_fingerprint = build_task_fingerprint(tasks)
    completed_allocation_fingerprint = (
        build_completed_allocation_fingerprint(allocations)
    )

    current_inputs = {
        "focus": focus_fingerprint,
        "tasks": task_fingerprint,
        "completed_allocations": completed_allocation_fingerprint,
    }

    force_rebuild = (
        os.environ.get("FORCE_REBUILD", "").lower() == "true"
    )

    changed = (
        current_inputs != {
            "focus": state.get("focus"),
            "tasks": state.get("tasks"),
            "completed_allocations": state.get("completed_allocations"),
        }
    )

    if not force_rebuild and not changed and not reconsider_requested:
        print("No relevant changes detected.")
        print("Scheduler finished without changing the Daily Plan.")
        return

    if reconsider_requested:
        print("Explicit Reconsider request: rebuilding now.")
    elif force_rebuild:
        print("Forced rebuild requested.")
    else:
        print("Relevant scheduling changes detected.")

    # Normal automatic rebuilds do not churn the active Focus Time block.
    # An explicit Reconsider request is different: it intentionally
    # overrides that protection. read_focus_time() has already clipped an
    # active block's start to NOW, so the rebuild sees only the remaining
    # future portion of the current block.
    if not reconsider_requested and should_defer_rebuild(all_focus_blocks):
        return

    rebuild(
        tasks,
        all_focus_blocks,
        focus_blocks,
        allocations,
    )

    save_state(current_inputs)

    # Only clear the button's request after the entire rebuild and state
    # save have succeeded. If anything fails, the checkbox remains checked
    # so the request is not silently lost.
    if reconsider_requested:
        clear_reconsider_request(
            schedule_status["page_id"],
            schedule_status["reconsider_property"],
        )

    print("Scheduler finished successfully.")


if __name__ == "__main__":
    main()
