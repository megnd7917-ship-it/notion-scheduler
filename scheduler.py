Today 11:06 AM
Pasted text(3).txt
Document
can you review this code for a scheduler. I want to double check that it's working the way I want before the school year starts and I don't have time to edit/change it. 
I would also like to change the structure of the scheduler slightly. I will be working a full time job with PFS, so i would like to remove the thirty hours a week necessity, that overcomplicates things. I've deleted the page on the database which has the thirty hours. please let me know if any of the database names don't match the page name
Pasted text(4).txt
Document
this is how it currently runs. while you review this, I will delete the master to do list as well as the automations which it's connected to. do i need to connect the smaller databases to the task allocations database in this case?
here are the properties. i've deleted the relation to the master to do list. should i add separate relations to the small to do lists, or how should i connect them.
i'm realizing that none of the tasks which are completed have been marked in their databases as complete. i want to be sure that a task isn't showing up twice. will i have to delete all existing task allocations? this may be good, and i can go through the small lists manually to check off all those tasks i've already done
Pasted text(5).txt
Document

Yes — I can see the current structure clearly now.

The important pieces are:

Each small To-Do database is the source of truth.
Each source task has Completed.
Task Allocations has the one-way relations to the individual source databases.
Hold lives on Task Allocations.
Focus time is its own database and is what the scheduler needs to work within.
There is no Master To-Do List anymore.
We want the scheduler to:
pull directly from the individual To-Do databases,
avoid allocating already-completed work,
avoid duplicate allocations,
mark the last allocation as (final) when a task has multiple allocations,
move (final) if the workload is subsequently increased,
preserve completed allocations,
update whenever the scheduler is rerun.

I also agree with your earlier instinct: we should keep the code as simple as possible. The previous version accumulated too many mechanisms.

One thing I need before we change anything

Please paste the current scheduler code from GitHub here.

I don't want to reconstruct it from memory and accidentally remove something that is currently working. Once you paste it, I'll review it against the structure shown in these screenshots and give you one complete replacement file that you can copy/paste into GitHub — not snippets or instructions for editing individual lines.

We can then proceed one change at a time, just like we did with the Notion cleanup.

Digital Ocean

Smart Model Selection

No code. Write rules in plain English.

Ad
More options
Pasted text(6).txt
Document
i've copied and pasted this from github, i clicked control a and then control c, so this should be the code. if it's not please let me know

Yes — you successfully copied the code, and I can see the complete file. It is 2,222 lines long, so this is not just a partial snippet.

However, do not paste this version back into GitHub yet. I found an important problem while reviewing it:

This is still the old architecture

The code still expects a database called “Master To-Do List.” In particular:

It explicitly searches for "Master To-Do List" as the source of all tasks.
It expects every Task Allocation to have a "Master To-Do List" relation.
When it creates a new allocation, it actually creates that old relation.
Its completed-work calculations are built around those Master To-Do relations.

That means this is the code you copied from GitHub, but it is not yet the code we want for the Notion structure you've just built.

The good news

A lot of the scheduler logic itself is useful and doesn't need to be reinvented. The main thing we need to change is the data architecture:

Current structure we want:

JST / Reader / Applications / COM 210 / ENL 248 / Independent Study / LSAT / PFS / Personal
↓
one-way relation from each task to Task Allocations
↓
Task Allocations

And each source database now has:

Completed
its existing workload/deadline/etc.
its one-way relation to Task Allocations

Task Allocations has the corresponding relations, plus:

Allocation
Completion
Focus time
Hold
Overdue
Schedule Order
Unit
etc.

So we need to modify the code so that Task Allocations identify their source task through those individual relations, rather than through the deleted Master To-Do List.

And importantly, we can keep this much simpler than the previous version. We don't need to introduce another master database or another layer of confirmation.

I would not change anything in GitHub yet. I have the full code now, so the next step is for me to rewrite this version around your current databases while preserving the scheduling logic that already works.

great. again, to be safe, i want to restate the need for this to be as simple as possible, while still doing what we want it to do. if there's an element of the old code you're not sure what it does, please ask what it was meant to do. otherwise, please send me the completed code

Yes. I reviewed the old code against the structure you've built and simplified the architecture rather than trying to patch the old Master To-Do system.

The version below is built around these nine source databases:

JST To-Do → JST Task
Reader To-Do → Reader Task
Applications To-Do → Applications Task
COM 210 To-Do → COM 210 Task
ENL 248 To-Do → ENL 248 Task
Independent Study To-Do → Independent Study Task
LSAT To-Do → LSAT Task
PFS To-Do → PFS Task
Personal To-Do → Personal Task

It also does the things we've settled on:

No Master To-Do List
Reads Completed from each source database.
Completed Task Allocations remain as historical records.
Incomplete, non-held allocations are rebuilt when something changes.
The scheduler can rebuild during an active Focus Time block.
Hold keeps an allocation visible but removes it from active scheduling and lets dependent tasks proceed.
No Confirmed property.
(final) is added only when a task has multiple active allocations.
If additional workload is added later, the old (final) is removed and moved to the new final allocation.
A task with only one allocation does not get (final).
Changing workload, completion, Focus Time, or Hold causes the scheduler's inputs to change and therefore triggers a rebuild on the next run.
The scheduler still updates the Schedule Status feedback even when it doesn't need to rebuild.
Dependencies still work across the nine databases.
The day-before-deadline planning target and actual-deadline behavior are retained.
PFS's weekly 30-hour target behavior is retained, with PFS identified by the PFS To-Do database rather than the deleted Project property.

I have not added another layer of databases, confirmations, or state that isn't necessary.

Here is the complete replacement file:

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

DEPENDENCY_PROPERTY_NAME = "Blocked by"

PFS_TASK_NAME = "PFS weekly hours"
PFS_DATABASE_NAME = "PFS To-Do"
PFS_WEEKLY_TARGET_MINUTES = 30 * 60

CREATE_REQUEST_DELAY = 0.5
MAX_RATE_LIMIT_RETRIES = 5

STATE_FILE = os.environ.get(
    "SCHEDULER_STATE_FILE",
    ".scheduler_state.json",
)


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

        data_source_id = get_data_source(
            database_id
        )

        pages = query_data_source(
            data_source_id
        )

        dependency_found = False

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

            if (
                dependency_property
                and dependency_property.get("type")
                == "relation"
            ):
                dependency_found = True

            workload = number_value(
                page,
                "Workload",
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
                "dependencies": relation_ids(
                    page,
                    DEPENDENCY_PROPERTY_NAME,
                ),
                "minutes": workload_to_minutes(
                    workload,
                    unit,
                ),
            })

        if not dependency_found:
            raise RuntimeError(
                f'"{database_name}" does not have '
                f'a "{DEPENDENCY_PROPERTY_NAME}" relation.'
            )

    print(
        f"Source tasks found: {len(tasks)}"
    )

    return tasks


# ============================================================
# FOCUS TIME
# ============================================================

def read_focus_time():
    database_id = find_database(
        "Focus time"
    )

    data_source_id = get_data_source(
        database_id
    )

    pages = query_data_source(
        data_source_id
    )

    now = datetime.now(TZ)

    horizon = (
        now
        + timedelta(days=PLANNING_DAYS)
    )

    all_blocks = []

    for page in pages:

        date = date_value(
            page,
            "Date",
        )

        if not date or not date["end"]:
            continue

        all_blocks.append({
            "page_id": page["id"],
            "start": date["start"],
            "end": date["end"],
            "last_edited_time": page.get(
                "last_edited_time"
            ),
        })

    all_blocks.sort(
        key=lambda block: block["start"]
    )

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

        minutes = int(
            (
                end - start
            ).total_seconds()
            / 60
        )

        if minutes <= 0:
            continue

        schedulable_blocks.append({
            "page_id": block["page_id"],
            "start": start,
            "end": end,
            "capacity": minutes,
            "remaining": minutes,
        })

    return (
        all_blocks,
        schedulable_blocks,
    )


# ============================================================
# TASK ALLOCATIONS
# ============================================================

def read_allocations():
    database_id = find_database(
        "Task Allocations"
    )

    data_source_id = get_data_source(
        database_id
    )

    pages = query_data_source(
        data_source_id
    )

    allocations = []

    for page in pages:

        source_links = {}

        for relation_name in SOURCE_DATABASES.values():

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
                "Name",
            ),
            "source_links": source_links,
            "focus_ids": relation_ids(
                page,
                "Focus time",
            ),
            "allocation": (
                number_value(
                    page,
                    "Allocation",
                )
                or 0
            ),
            "unit": select_value(
                page,
                "Unit",
            ),
            "completed": checkbox_value(
                page,
                "Completion",
            ),
            "overdue": checkbox_value(
                page,
                "Overdue",
            ),
            "hold": checkbox_value(
                page,
                "Hold",
            ),
        })

    return allocations


def allocation_task_id(
    allocation,
    tasks_by_id,
):
    normalized_ids = {
        normalize_notion_id(page_id): page_id
        for page_id in tasks_by_id
    }

    matches = []

    for ids in allocation[
        "source_links"
    ].values():

        for raw_id in ids:

            task_id = normalized_ids.get(
                normalize_notion_id(raw_id)
            )

            if task_id:
                matches.append(task_id)

    if not matches:
        return None

    if len(matches) > 1:
        names = [
            tasks_by_id[task_id]["task"]
            for task_id in matches
        ]

        print(
            f'Warning: allocation '
            f'"{allocation["name"]}" is linked '
            f"to multiple tasks: "
            f"{', '.join(names)}. "
            f"It will be credited once."
        )

    return matches[0]


def allocation_minutes(
    allocation,
    task,
):
    unit = (
        allocation.get("unit")
        or task["unit"]
    )

    return workload_to_minutes(
        allocation["allocation"],
        unit,
    )


def calculate_completed_work(
    allocations,
    tasks_by_id,
):
    completed = {}

    for allocation in allocations:

        if not allocation["completed"]:
            continue

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            continue

        minutes = allocation_minutes(
            allocation,
            tasks_by_id[task_id],
        )

        completed[task_id] = (
            completed.get(task_id, 0)
            + minutes
        )

    return completed


# ============================================================
# HOLD
# ============================================================

def held_task_ids(
    allocations,
    tasks_by_id,
):
    held = set()

    for allocation in allocations:

        if allocation["completed"]:
            continue

        if not allocation["hold"]:
            continue

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if task_id:
            held.add(task_id)

    return held


# ============================================================
# PFS
# ============================================================

def current_week_range():
    today = datetime.now(TZ).date()

    monday = (
        today
        - timedelta(days=today.weekday())
    )

    return (
        monday,
        monday + timedelta(days=7),
    )


def calculate_completed_pfs_minutes(
    allocations,
    tasks_by_id,
    all_focus_blocks,
):
    week_start, week_end = (
        current_week_range()
    )

    blocks_by_id = {
        block["page_id"]: block
        for block in all_focus_blocks
    }

    total = 0

    for allocation in allocations:

        if not allocation["completed"]:
            continue

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            continue

        task = tasks_by_id[task_id]

        if task["database"] != PFS_DATABASE_NAME:
            continue

        if task["task"] == PFS_TASK_NAME:
            continue

        in_current_week = any(
            focus_id in blocks_by_id
            and
            week_start
            <= blocks_by_id[
                focus_id
            ]["start"].date()
            <
            week_end
            for focus_id
            in allocation["focus_ids"]
        )

        if in_current_week:
            total += allocation_minutes(
                allocation,
                task,
            )

    return total


# ============================================================
# DEPENDENCIES
# ============================================================

def build_dependency_graph(tasks):
    tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    normalized_ids = {
        normalize_notion_id(task_id): task_id
        for task_id in tasks_by_id
    }

    dependents = {
        task["page_id"]: set()
        for task in tasks
    }

    for task in tasks:

        valid_dependencies = set()

        for raw_dependency_id in task[
            "dependencies"
        ]:

            dependency_id = normalized_ids.get(
                normalize_notion_id(
                    raw_dependency_id
                )
            )

            if dependency_id == task["page_id"]:
                raise RuntimeError(
                    f'Task "{task["task"]}" '
                    f"depends on itself."
                )

            if not dependency_id:
                print(
                    f'Warning: dependency on an '
                    f'unavailable page was ignored '
                    f'for "{task["task"]}".'
                )
                continue

            valid_dependencies.add(
                dependency_id
            )

            dependents[
                dependency_id
            ].add(
                task["page_id"]
            )

        task["dependencies"] = sorted(
            valid_dependencies
        )

    visiting = set()
    visited = set()

    def visit(task_id, path):

        if task_id in visiting:

            cycle_start = path.index(
                task_id
            )

            cycle_ids = (
                path[cycle_start:]
                + [task_id]
            )

            cycle_names = [
                tasks_by_id[
                    task_id
                ]["task"]
                for task_id in cycle_ids
            ]

            raise RuntimeError(
                "Circular dependency detected: "
                + " -> ".join(cycle_names)
            )

        if task_id in visited:
            return

        visiting.add(task_id)

        for dependency_id in tasks_by_id[
            task_id
        ]["dependencies"]:

            visit(
                dependency_id,
                path + [dependency_id],
            )

        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(
            task["page_id"],
            [task["page_id"]],
        )

    return (
        tasks_by_id,
        dependents,
    )


def task_planning_deadline(task):
    if not task["deadline"]:
        return None

    return (
        task["deadline"]["start"]
        - timedelta(days=1)
    )


def actual_deadline_end(task):
    deadline = task.get("deadline")

    if not deadline:
        return None

    if deadline.get("end"):
        return deadline["end"]

    return deadline["start"].replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=999999,
    )


def deadline_day_kind(task, now):
    deadline = actual_deadline_end(task)

    if not deadline:
        return None

    today = now.date()

    deadline_date = deadline.date()

    if deadline_date < today:
        return "overdue"

    if deadline_date == today:
        return "today"

    return "future"


def calculate_dependency_deadlines(
    tasks,
    remaining,
    held_ids,
):
    tasks_by_id, dependents = (
        build_dependency_graph(tasks)
    )

    effective = {
        task_id: task_planning_deadline(task)
        for task_id, task
        in tasks_by_id.items()
    }

    visiting = set()

    def solve(task_id):

        if task_id in visiting:
            raise RuntimeError(
                "Circular dependency detected."
            )

        visiting.add(task_id)

        for dependent_id in dependents[
            task_id
        ]:

            solve(dependent_id)

            dependent = tasks_by_id[
                dependent_id
            ]

            if dependent["completed"]:
                continue

            if dependent_id in held_ids:
                continue

            dependent_remaining = remaining.get(
                dependent_id,
                0,
            )

            if dependent_remaining <= 0:
                continue

            downstream_deadline = effective.get(
                dependent_id
            )

            if downstream_deadline is None:
                continue

            candidate = (
                downstream_deadline
                - timedelta(
                    minutes=dependent_remaining
                )
                - timedelta(days=1)
            )

            own_deadline = effective.get(
                task_id
            )

            if (
                own_deadline is None
                or candidate < own_deadline
            ):
                effective[task_id] = candidate

        visiting.remove(task_id)

    for task in tasks:
        solve(task["page_id"])

    for task in tasks:
        task["effective_deadline"] = (
            effective.get(
                task["page_id"]
            )
        )

    return (
        tasks_by_id,
        dependents,
    )


def dependency_blocked(
    task,
    tasks_by_id,
    completed,
    held_ids,
):
    for dependency_id in task[
        "dependencies"
    ]:

        dependency = tasks_by_id.get(
            dependency_id
        )

        if not dependency:
            continue

        if dependency["completed"]:
            continue

        # A held prerequisite is temporarily
        # ignored so the dependent can proceed.
        if dependency_id in held_ids:
            continue

        if (
            completed.get(
                dependency_id,
                0,
            )
            >= dependency["minutes"]
        ):
            continue

        return True

    return False


def dependency_depth(
    task_id,
    tasks_by_id,
    cache,
):
    if task_id in cache:
        return cache[task_id]

    task = tasks_by_id[task_id]

    if not task["dependencies"]:
        cache[task_id] = 0
        return 0

    valid_dependencies = [
        dependency_id
        for dependency_id
        in task["dependencies"]
        if dependency_id in tasks_by_id
    ]

    if not valid_dependencies:
        cache[task_id] = 0
        return 0

    depth = (
        1
        + max(
            dependency_depth(
                dependency_id,
                tasks_by_id,
                cache,
            )
            for dependency_id
            in valid_dependencies
        )
    )

    cache[task_id] = depth

    return depth


# ============================================================
# SCHEDULING RULES
# ============================================================

def priority_multiplier(priority):
    if priority == "High":
        return 1.20

    if priority == "Medium":
        return 1.05

    return 1.00


def hours_until_effective_deadline(
    task,
    now,
):
    deadline = task.get(
        "effective_deadline"
    )

    if not deadline:
        return None

    return (
        deadline - now
    ).total_seconds() / 3600


def task_score(
    task,
    remaining_minutes,
    now,
    same_day_minutes,
    dependency_depth_value,
):
    hours = hours_until_effective_deadline(
        task,
        now,
    )

    deadline_kind = deadline_day_kind(
        task,
        now,
    )

    is_low = (
        task.get("priority")
        == "Low"
    )

    if (
        deadline_kind == "overdue"
        and not is_low
    ):
        deadline_score = 1_000_000_000

    elif (
        deadline_kind == "today"
        and not is_low
    ):
        deadline_score = 500_000_000

    elif hours is None:
        deadline_score = 0.1

    elif hours <= 0 and not is_low:
        deadline_score = 100_000_000

    elif hours <= 0:
        deadline_score = 0.5

    else:
        deadline_score = (
            100
            / ((hours + 1) ** 2)
        )

    score = (
        deadline_score
        * priority_multiplier(
            task["priority"]
        )
    )

    score += min(
        2.0,
        remaining_minutes / 120,
    )

    if dependency_depth_value > 0:
        score *= (
            1
            + min(
                0.20,
                0.05
                * dependency_depth_value,
            )
        )

    if same_day_minutes > 0:
        score *= 0.85

    return score


def task_is_eligible(
    task,
    remaining_minutes,
    block_start,
    now,
    tasks_by_id,
    completed,
    held_ids,
):
    if remaining_minutes <= 0:
        return False

    if task["completed"]:
        return False

    if task["page_id"] in held_ids:
        return False

    if dependency_blocked(
        task,
        tasks_by_id,
        completed,
        held_ids,
    ):
        return False

    actual_deadline = actual_deadline_end(
        task
    )

    deadline_kind = deadline_day_kind(
        task,
        now,
    )

    effective_deadline = task.get(
        "effective_deadline"
    )

    # High/Medium overdue work can be
    # scheduled in any future Focus Time.
    if (
        deadline_kind == "overdue"
        and task["priority"] != "Low"
    ):
        return True

    # High/Medium work due today is
    # scheduled on the deadline day.
    if (
        deadline_kind == "today"
        and task["priority"] != "Low"
    ):
        return (
            block_start.date()
            == now.date()
        )

    if not effective_deadline:
        return True

    if effective_deadline <= now:

        if (
            actual_deadline
            and block_start >= actual_deadline
        ):
            return False

        return True

    if (
        block_start
        >= effective_deadline
    ):
        return False

    if (
        actual_deadline
        and block_start >= actual_deadline
    ):
        return False

    return True


def choose_allocation_size(
    task,
    remaining_minutes,
    available_minutes,
    block_start,
    now,
):
    maximum = min(
        remaining_minutes,
        available_minutes,
    )

    barriers = []

    if (
        task.get("effective_deadline")
        and task["effective_deadline"] > now
    ):
        barriers.append(
            task["effective_deadline"]
        )

    actual_deadline = actual_deadline_end(
        task
    )

    if (
        actual_deadline
        and actual_deadline > now
    ):
        barriers.append(
            actual_deadline
        )

    if barriers:

        earliest_barrier = min(
            barriers
        )

        deadline_capacity = int(
            (
                earliest_barrier
                - block_start
            ).total_seconds()
            / 60
        )

        maximum = min(
            maximum,
            deadline_capacity,
        )

    if maximum <= 0:
        return 0

    deadline_forces_whole_task = (
        task["priority"] != "Low"
        and deadline_day_kind(
            task,
            now,
        ) in {
            "overdue",
            "today",
        }
    )

    if (
        task["continuous"]
        or deadline_forces_whole_task
    ):

        if remaining_minutes <= maximum:
            return remaining_minutes

        return 0

    if maximum <= PREFERRED_MAX_CHUNK_MINUTES:
        return maximum

    remainder = (
        remaining_minutes
        - PREFERRED_MAX_CHUNK_MINUTES
    )

    if (
        0 < remainder
        <= MIN_CHUNK_MINUTES
    ):
        return remaining_minutes

    return PREFERRED_MAX_CHUNK_MINUTES


# ============================================================
# PFS SCHEDULING
# ============================================================

def pfs_score(
    block,
    remaining_target,
    week_end,
):
    block_date = block["start"].date()

    is_weekday = (
        block_date.weekday() < 5
    )

    days_left = max(
        1,
        (week_end - block_date).days,
    )

    score = (
        0.35
        if is_weekday
        else 0.20
    )

    score += min(
        2.0,
        7.0 / days_left,
    )

    deficit_hours = (
        remaining_target / 60
    )

    score += min(
        2.5,
        deficit_hours / 12,
    )

    return score


# ============================================================
# SCHEDULE TASKS
# ============================================================

def schedule_tasks(
    tasks,
    focus_blocks,
    completed,
    pfs_task,
    completed_pfs_minutes,
    held_ids,
):
    now = datetime.now(TZ)

    remaining = {}

    for task in tasks:

        if task["completed"]:
            continue

        if task["task"] == PFS_TASK_NAME:
            continue

        if task["page_id"] in held_ids:
            continue

        remaining[
            task["page_id"]
        ] = max(
            0,
            task["minutes"]
            - completed.get(
                task["page_id"],
                0,
            ),
        )

    tasks_by_id, _ = (
        build_dependency_graph(
            tasks
        )
    )

    calculate_dependency_deadlines(
        tasks,
        remaining,
        held_ids,
    )

    dependency_depth_cache = {}

    for task in tasks:
        task["dependency_depth"] = (
            dependency_depth(
                task["page_id"],
                tasks_by_id,
                dependency_depth_cache,
            )
        )

    pfs_remaining_target = max(
        0,
        PFS_WEEKLY_TARGET_MINUTES
        - completed_pfs_minutes,
    )

    new_allocations = []

    for block in focus_blocks:

        excluded_for_block = set()

        while (
            block["remaining"]
            >= MIN_CHUNK_MINUTES
        ):

            candidates = []

            for task in tasks:

                task_id = task["page_id"]

                if task["task"] == PFS_TASK_NAME:
                    continue

                if task["completed"]:
                    continue

                if task_id in held_ids:
                    continue

                if task_id in excluded_for_block:
                    continue

                rem = remaining.get(
                    task_id,
                    0,
                )

                if rem <= 0:
                    continue

                if not task_is_eligible(
                    task,
                    rem,
                    block["start"],
                    datetime.now(TZ),
                    tasks_by_id,
                    completed,
                    held_ids,
                ):
                    continue

                same_day_minutes = sum(
                    item["amount_minutes"]
                    for item in new_allocations
                    if (
                        item["task"]["page_id"]
                        == task_id
                        and
                        item["focus_page_start"].date()
                        == block["start"].date()
                    )
                )

                score = task_score(
                    task,
                    rem,
                    now,
                    same_day_minutes,
                    task.get(
                        "dependency_depth",
                        0,
                    ),
                )

                candidates.append(
                    (
                        "general",
                        score,
                        task,
                    )
                )

            if (
                pfs_task
                and pfs_task["page_id"]
                not in held_ids
                and pfs_remaining_target
                >= MIN_CHUNK_MINUTES
                and
                current_week_range()[0]
                <= block["start"].date()
                <
                current_week_range()[1]
            ):

                candidates.append(
                    (
                        "pfs",
                        pfs_score(
                            block,
                            pfs_remaining_target,
                            current_week_range()[1],
                        ),
                        pfs_task,
                    )
                )

            if not candidates:
                break

            urgent = [
                candidate
                for candidate
                in candidates
                if (
                    candidate[0] == "general"
                    and
                    candidate[2].get(
                        "effective_deadline"
                    )
                    and
                    candidate[2][
                        "effective_deadline"
                    ] <= now
                )
            ]

            if urgent:
                candidates = urgent

            candidates.sort(
                key=lambda item: item[1],
                reverse=True,
            )

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
                    "focus_page_start": block["start"],
                })

                block["remaining"] -= amount
                pfs_remaining_target -= amount

                continue

            amount = choose_allocation_size(
                chosen,
                remaining[
                    chosen["page_id"]
                ],
                block["remaining"],
                block["start"],
                now,
            )

            if amount <= 0:

                excluded_for_block.add(
                    chosen["page_id"]
                )

                continue

            new_allocations.append({
                "task": chosen,
                "amount_minutes": amount,
                "focus_page_id": block["page_id"],
                "focus_page_start": block["start"],
            })

            block["remaining"] -= amount

            remaining[
                chosen["page_id"]
            ] -= amount

    return (
        new_allocations,
        remaining,
    )


def assign_schedule_order(
    new_allocations,
    now,
):
    def urgency_key(item):

        task = item["task"]

        kind = deadline_day_kind(
            task,
            now,
        )

        priority_rank = {
            "High": 0,
            "Medium": 1,
            "Low": 3,
        }.get(
            task.get("priority"),
            2,
        )

        if (
            kind == "overdue"
            and task.get("priority")
            != "Low"
        ):
            tier = 0

        elif (
            kind == "today"
            and task.get("priority")
            != "Low"
        ):
            tier = 1

        elif kind == "overdue":
            tier = 4

        elif kind == "today":
            tier = 5

        else:
            tier = 2

        effective = task.get(
            "effective_deadline"
        )

        effective_key = (
            effective.timestamp()
            if effective
            else float("inf")
        )

        block_key = (
            item["focus_page_start"]
            .timestamp()
        )

        return (
            tier,
            priority_rank,
            effective_key,
            block_key,
            task["task"].lower(),
        )

    ordered = sorted(
        new_allocations,
        key=urgency_key,
    )

    for order, allocation in enumerate(
        ordered,
        start=1,
    ):

        allocation[
            "schedule_order"
        ] = order

        allocation[
            "overdue"
        ] = (
            deadline_day_kind(
                allocation["task"],
                now,
            )
            == "overdue"
        )


# ============================================================
# CREATE ALLOCATION
# ============================================================

def create_allocation(
    allocation,
):
    database_id = find_database(
        "Task Allocations"
    )

    data_source_id = get_data_source(
        database_id
    )

    task = allocation["task"]

    if task["task"] == PFS_TASK_NAME:
        unit = "Hours"
    else:
        unit = task["unit"]

    amount_minutes = (
        allocation["amount_minutes"]
    )

    display_amount = format_allocation(
        amount_minutes,
        unit,
    )

    name = (
        f'{task["task"]} — '
        f"{display_amount}"
    )

    relation_name = SOURCE_DATABASES[
        task["database"]
    ]

    properties = {
        "Name": {
            "title": [
                {
                    "text": {
                        "content": name
                    }
                }
            ]
        },

        "Schedule Order": {
            "number": allocation.get(
                "schedule_order",
                0,
            )
        },

        "Overdue": {
            "checkbox": allocation.get(
                "overdue",
                False,
            )
        },

        "Focus time": {
            "relation": [
                {
                    "id": allocation[
                        "focus_page_id"
                    ]
                }
            ]
        },

        "Allocation": {
            "number": minutes_to_units(
                amount_minutes,
                unit,
            )
        },

        "Unit": {
            "select": {
                "name": unit
            }
        },

        "Completion": {
            "checkbox": False
        },

        relation_name: {
            "relation": [
                {
                    "id": task["page_id"]
                }
            ]
        },
    }

    result = notion(
        "POST",
        "pages",
        json={
            "parent": {
                "data_source_id":
                    data_source_id
            },
            "properties": properties,
        },
    )

    time.sleep(
        CREATE_REQUEST_DELAY
    )

    return result


# ============================================================
# FINAL ALLOCATION LABEL
# ============================================================

def remove_final_label(name):
    return (
        name
        .replace(" (final)", "")
        .rstrip()
    )


def allocation_focus_start(
    allocation,
    blocks_by_id,
):
    starts = []

    for focus_id in allocation[
        "focus_ids"
    ]:

        block = blocks_by_id.get(
            focus_id
        )

        if block:
            starts.append(
                block["start"]
            )

    if starts:
        return max(starts)

    return datetime.min.replace(
        tzinfo=TZ
    )


def update_final_labels(
    allocations,
    tasks_by_id,
    all_focus_blocks,
):
    blocks_by_id = {
        block["page_id"]: block
        for block in all_focus_blocks
    }

    grouped = {}

    for allocation in allocations:

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        if not task_id:
            continue

        grouped.setdefault(
            task_id,
            [],
        ).append(
            allocation
        )

    changed = 0

    for task_id, task_allocations in grouped.items():

        # Held allocations are visible, but are
        # not part of the active scheduled sequence.
        active_allocations = [
            allocation
            for allocation
            in task_allocations
            if not allocation["hold"]
        ]

        final_page_id = None

        # "(final)" exists only when there are
        # multiple active allocations.
        if len(active_allocations) > 1:

            final_allocation = max(
                active_allocations,
                key=lambda allocation:
                    allocation_focus_start(
                        allocation,
                        blocks_by_id,
                    ),
            )

            final_page_id = (
                final_allocation["page_id"]
            )

        for allocation in task_allocations:

            current_name = (
                allocation["name"]
            )

            base_name = remove_final_label(
                current_name
            )

            if (
                final_page_id
                == allocation["page_id"]
            ):
                desired_name = (
                    f"{base_name} (final)"
                )
            else:
                desired_name = base_name

            if (
                desired_name
                == current_name
            ):
                continue

            notion(
                "PATCH",
                f"pages/{allocation['page_id']}",
                json={
                    "properties": {
                        "Name": {
                            "title": [
                                {
                                    "text": {
                                        "content":
                                            desired_name
                                    }
                                }
                            ]
                        }
                    }
                },
            )

            allocation[
                "name"
            ] = desired_name

            changed += 1

    if changed:
        print(
            "Updated '(final)' label on "
            f"{changed} allocation(s)."
        )


# ============================================================
# OVERDUE FLAGS
# ============================================================

def update_overdue_flags(
    tasks,
    allocations,
    completed,
):
    today = datetime.now(
        TZ
    ).date()

    tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    overdue_task_ids = set()

    for task in tasks:

        if task["completed"]:
            continue

        if task["task"] == PFS_TASK_NAME:
            continue

        remaining = max(
            0,
            task["minutes"]
            - completed.get(
                task["page_id"],
                0,
            ),
        )

        deadline = task.get(
            "deadline"
        )

        if (
            remaining > 0
            and deadline
            and deadline["start"].date()
            < today
        ):
            overdue_task_ids.add(
                task["page_id"]
            )

    changed = 0

    for allocation in allocations:

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        should_be_overdue = (
            not allocation["completed"]
            and task_id
            in overdue_task_ids
        )

        if (
            allocation["overdue"]
            == should_be_overdue
        ):
            continue

        notion(
            "PATCH",
            f"pages/{allocation['page_id']}",
            json={
                "properties": {
                    "Overdue": {
                        "checkbox":
                            should_be_overdue
                    }
                }
            },
        )

        allocation[
            "overdue"
        ] = should_be_overdue

        changed += 1

    if changed:
        print(
            f"Updated Overdue on "
            f"{changed} allocation(s)."
        )


# ============================================================
# SCHEDULE STATUS
# ============================================================

def calculate_status(
    tasks,
    completed,
    focus_blocks,
    completed_pfs_minutes,
    held_ids,
):
    now = datetime.now(TZ)

    horizon = (
        now
        + timedelta(days=PLANNING_DAYS)
    )

    available = sum(
        block["remaining"]
        for block in focus_blocks
    )

    remaining = {}

    total_remaining = 0

    for task in tasks:

        if task["completed"]:
            continue

        if task["task"] == PFS_TASK_NAME:
            continue

        if task["page_id"] in held_ids:
            continue

        rem = max(
            0,
            task["minutes"]
            - completed.get(
                task["page_id"],
                0,
            ),
        )

        if rem > 0:

            remaining[
                task["page_id"]
            ] = rem

            total_remaining += rem

    tasks_by_id, dependents = (
        build_dependency_graph(tasks)
    )

    required_by = {}

    for task in tasks:

        if task["completed"]:
            continue

        if task["task"] == PFS_TASK_NAME:
            continue

        if task["page_id"] in held_ids:
            continue

        if task["priority"] == "Low":
            continue

        deadline = actual_deadline_end(
            task
        )

        if deadline is not None:
            required_by[
                task["page_id"]
            ] = deadline

    visiting = set()

    def solve_required_by(task_id):

        if task_id in visiting:
            raise RuntimeError(
                "Circular dependency detected "
                "while calculating status."
            )

        visiting.add(task_id)

        for dependent_id in dependents.get(
            task_id,
            [],
        ):

            if dependent_id in held_ids:
                continue

            solve_required_by(
                dependent_id
            )

            dependent = tasks_by_id[
                dependent_id
            ]

            dependent_remaining = remaining.get(
                dependent_id,
                0,
            )

            if (
                dependent["completed"]
                or dependent_remaining <= 0
            ):
                continue

            downstream = required_by.get(
                dependent_id
            )

            if downstream is None:
                continue

            candidate = (
                downstream
                - timedelta(
                    minutes=dependent_remaining
                )
                - timedelta(days=1)
            )

            own = required_by.get(
                task_id
            )

            if (
                own is None
                or candidate < own
            ):
                required_by[
                    task_id
                ] = candidate

        visiting.remove(task_id)

    for task in tasks:
        solve_required_by(
            task["page_id"]
        )

    deadline_tasks = []

    for task_id, deadline in (
        required_by.items()
    ):

        if task_id not in remaining:
            continue

        if deadline > horizon:
            continue

        deadline_tasks.append(
            (
                deadline,
                remaining[task_id],
                tasks_by_id[task_id],
            )
        )

    deadline_tasks.sort(
        key=lambda item: item[0]
    )

    cumulative_required = 0

    worst_shortfall = 0

    worst_slack = None

    for (
        deadline,
        rem,
        task,
    ) in deadline_tasks:

        cumulative_required += rem

        capacity_through_deadline = 0

        for block in focus_blocks:

            if (
                block["start"]
                >= deadline
            ):
                continue

            usable_end = min(
                block["end"],
                deadline,
            )

            block_minutes = max(
                0,
                int(
                    (
                        usable_end
                        - block["start"]
                    ).total_seconds()
                    / 60
                ),
            )

            capacity_through_deadline += min(
                block["remaining"],
                block_minutes,
            )

        slack = (
            capacity_through_deadline
            - cumulative_required
        )

        if (
            worst_slack is None
            or slack < worst_slack
        ):
            worst_slack = slack

        if slack < 0:
            worst_shortfall = max(
                worst_shortfall,
                abs(slack),
            )

    pfs_active = any(
        task["task"] == PFS_TASK_NAME
        and not task["completed"]
        for task in tasks
    )

    pfs_required = 0

    if pfs_active:
        pfs_required = max(
            0,
            PFS_WEEKLY_TARGET_MINUTES
            - completed_pfs_minutes,
        )

    if worst_shortfall > 0:

        status = (
            "🟠 Needs attention"
        )

        status_detail = (
            "🟠 Needs attention — "
            f"{format_minutes(worst_shortfall)} "
            "additional Focus Time needed "
            "to meet an actual "
            "High/Medium-priority deadline"
        )

        difference = -worst_shortfall

    else:

        status = "🟢 On track"

        if worst_slack is None:

            difference = available

            status_detail = (
                "🟢 On track — no hard "
                "actual deadlines currently "
                "require additional Focus Time"
            )

        else:

            difference = worst_slack

            status_detail = (
                "🟢 On track — minimum "
                "hard-deadline slack is "
                f"{format_minutes(worst_slack)}"
            )

    return {
        "status": status,
        "status_detail": status_detail,
        "available": available,
        "deadline_required": sum(
            rem
            for _, rem, _
            in deadline_tasks
        ),
        "pfs_required": pfs_required,
        "total_remaining": total_remaining,
        "difference": difference,
        "pfs_active": pfs_active,
    }


def update_schedule_status(
    tasks,
    allocations,
    focus_blocks,
    completed,
    completed_pfs_minutes,
    held_ids,
):
    status_info = calculate_status(
        tasks,
        completed,
        focus_blocks,
        completed_pfs_minutes,
        held_ids,
    )

    database_id = find_database(
        "Schedule Status"
    )

    data_source_id = get_data_source(
        database_id
    )

    pages = query_data_source(
        data_source_id
    )

    current_schedule_page = None

    for page in pages:

        if (
            title_value(
                page,
                "Name",
            ).strip()
            == "Current Schedule"
        ):
            current_schedule_page = page
            break

    if not current_schedule_page:
        raise RuntimeError(
            'Could not find the '
            '"Current Schedule" page in '
            '"Schedule Status".'
        )

    actual_property_names = set(
        current_schedule_page
        .get("properties", {})
        .keys()
    )

    def resolve_property(
        expected_name
    ):
        if expected_name in (
            actual_property_names
        ):
            return expected_name

        def normalize(name):
            return "".join(
                character.lower()
                for character in name
                if character.isalnum()
            )

        expected_normalized = normalize(
            expected_name
        )

        for actual_name in (
            actual_property_names
        ):

            if (
                normalize(actual_name)
                == expected_normalized
            ):
                return actual_name

        raise RuntimeError(
            f'Could not find Schedule Status '
            f'property "{expected_name}".'
        )

    status_property = resolve_property(
        "Status"
    )

    deadline_property = resolve_property(
        "Near-Term Deadline Work"
    )

    pfs_property = resolve_property(
        "PFS Weekly Target Remaining"
    )

    capacity_property = resolve_property(
        "Schedule Capacity"
    )

    difference_property = resolve_property(
        "Schedule difference"
    )

    updated_property = resolve_property(
        "Last Updated"
    )

    reconsider_property = resolve_property(
        "Reconsider"
    )

    reconsider_requested = checkbox_value(
        current_schedule_page,
        reconsider_property,
    )

    now = datetime.now(TZ)

    properties = {
        status_property: {
            "select": {
                "name":
                    status_info["status"]
            }
        },

        deadline_property: {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content":
                            format_minutes(
                                status_info[
                                    "deadline_required"
                                ]
                            )
                    },
                }
            ]
        },

        pfs_property: {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": (
                            format_minutes(
                                status_info[
                                    "pfs_required"
                                ]
                            )
                            if
                            status_info[
                                "pfs_active"
                            ]
                            else
                            "Inactive"
                        )
                    },
                }
            ]
        },

        capacity_property: {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content":
                            format_minutes(
                                status_info[
                                    "available"
                                ]
                            )
                    },
                }
            ]
        },

        difference_property: {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": (
                            f"+{format_minutes(status_info['difference'])} surplus"
                            if
                            status_info[
                                "difference"
                            ] >= 0
                            else
                            f"-{format_minutes(abs(status_info['difference']))} needed"
                        )
                    },
                }
            ]
        },

        updated_property: {
            "date": {
                "start":
                    now.isoformat()
            }
        },
    }

    notion(
        "PATCH",
        f"pages/{current_schedule_page['id']}",
        json={
            "properties":
                properties
        },
    )

    print(
        "Schedule Status updated."
    )

    print(
        f"  {status_info['status_detail']}"
    )

    return {
        "status_info": status_info,
        "page_id":
            current_schedule_page["id"],
        "reconsider_requested":
            reconsider_requested,
        "reconsider_property":
            reconsider_property,
    }


def clear_reconsider_request(
    page_id,
    property_name,
):
    notion(
        "PATCH",
        f"pages/{page_id}",
        json={
            "properties": {
                property_name: {
                    "checkbox": False
                }
            }
        },
    )

    print(
        "Reconsider request completed."
    )


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

    return hashlib.sha256(
        encoded
    ).hexdigest()


def build_task_fingerprint(tasks):
    return stable_hash([
        (
            task["page_id"],
            task["task"],
            task["database"],

            (
                task["deadline"]["start"]
                .isoformat()
                if task["deadline"]
                else None
            ),

            (
                task["deadline"]["end"]
                .isoformat()
                if (
                    task["deadline"]
                    and
                    task["deadline"]["end"]
                )
                else None
            ),

            task["workload"],
            task["unit"],
            task["priority"],
            task["continuous"],
            task["completed"],
            tuple(
                sorted(
                    task["dependencies"]
                )
            ),
        )

        for task in sorted(
            tasks,
            key=lambda task:
                task["page_id"],
        )
    ])


def build_focus_fingerprint(
    all_focus_blocks
):
    horizon = (
        datetime.now(TZ)
        + timedelta(
            days=PLANNING_DAYS
        )
    )

    return stable_hash([
        (
            block["page_id"],
            block["start"].isoformat(),
            block["end"].isoformat(),
            block.get(
                "last_edited_time"
            ),
        )

        for block in all_focus_blocks

        if block["start"] < horizon
    ])


def build_allocation_fingerprint(
    allocations,
    tasks_by_id,
):
    rows = []

    for allocation in allocations:

        task_id = allocation_task_id(
            allocation,
            tasks_by_id,
        )

        rows.append(
            (
                allocation["page_id"],
                task_id,
                allocation["completed"],
                allocation["allocation"],
                allocation["unit"],
                allocation["hold"],
            )
        )

    return stable_hash(
        sorted(rows)
    )


def load_state():
    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as file:

            return json.load(file)

    except FileNotFoundError:

        return {}

    except json.JSONDecodeError:

        print(
            "Warning: scheduler state file "
            "is invalid. Rebuilding."
        )

        return {}


def save_state(state):
    temporary_file = (
        STATE_FILE + ".tmp"
    )

    with open(
        temporary_file,
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
        temporary_file,
        STATE_FILE,
    )


# ============================================================
# REBUILD
# ============================================================

def delete_incomplete_allocations(
    allocations
):
    to_delete = [
        allocation
        for allocation in allocations
        if (
            not allocation["completed"]
            and not allocation["hold"]
        )
    ]

    if not to_delete:
        print(
            "No provisional allocations "
            "to remove."
        )
        return

    print(
        f"Removing {len(to_delete)} "
        "provisional allocation(s)..."
    )

    for allocation in to_delete:

        archive_page(
            allocation["page_id"]
        )


def rebuild(
    tasks,
    all_focus_blocks,
    focus_blocks,
    allocations,
):
    print()
    print(
        "Rebuilding future schedule..."
    )

    tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    completed = (
        calculate_completed_work(
            allocations,
            tasks_by_id,
        )
    )

    held_ids = held_task_ids(
        allocations,
        tasks_by_id,
    )

    # Validate dependencies before
    # deleting provisional allocations.
    build_dependency_graph(tasks)

    completed_pfs_minutes = (
        calculate_completed_pfs_minutes(
            allocations,
            tasks_by_id,
            all_focus_blocks,
        )
    )

    delete_incomplete_allocations(
        allocations
    )

    pfs_task = next(
        (
            task
            for task in tasks
            if (
                task["task"]
                == PFS_TASK_NAME
                and not task["completed"]
            )
        ),
        None,
    )

    status_info = calculate_status(
        tasks,
        completed,
        focus_blocks,
        completed_pfs_minutes,
        held_ids,
    )

    print()
    print(
        "----------------------------------------"
    )

    print(
        status_info["status_detail"]
    )

    print(
        "----------------------------------------"
    )

    new_allocations, _ = schedule_tasks(
        tasks,
        focus_blocks,
        completed,
        pfs_task,
        completed_pfs_minutes,
        held_ids,
    )

    assign_schedule_order(
        new_allocations,
        datetime.now(TZ),
    )

    print(
        f"New allocations to create: "
        f"{len(new_allocations)}"
    )

    for allocation in new_allocations:

        task = allocation["task"]

        unit = (
            "Hours"
            if task["task"] == PFS_TASK_NAME
            else task["unit"]
        )

        print(
            f'  {task["task"]} → '
            f'{format_allocation(
                allocation["amount_minutes"],
                unit
            )}'
        )

        create_allocation(
            allocation
        )

    # Read the database again so the newly
    # created allocations are included when
    # deciding which allocation is final.
    refreshed_allocations = (
        read_allocations()
    )

    update_final_labels(
        refreshed_allocations,
        tasks_by_id,
        all_focus_blocks,
    )

    return {
        "completed_pfs_minutes":
            completed_pfs_minutes,
        "new_allocations":
            len(new_allocations),
    }


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "========================================"
    )

    print(
        "          NOTION SCHEDULER"
    )

    print(
        "========================================"
    )

    print()

    tasks = read_tasks()

    all_focus_blocks, focus_blocks = (
        read_focus_time()
    )

    allocations = read_allocations()

    print(
        f"Focus Time blocks found: "
        f"{len(focus_blocks)}"
    )

    print(
        f"Task Allocations found: "
        f"{len(allocations)}"
    )

    tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    completed = (
        calculate_completed_work(
            allocations,
            tasks_by_id,
        )
    )

    completed_pfs_minutes = (
        calculate_completed_pfs_minutes(
            allocations,
            tasks_by_id,
            all_focus_blocks,
        )
    )

    held_ids = held_task_ids(
        allocations,
        tasks_by_id,
    )

    update_overdue_flags(
        tasks,
        allocations,
        completed,
    )

    schedule_status = (
        update_schedule_status(
            tasks,
            allocations,
            focus_blocks,
            completed,
            completed_pfs_minutes,
            held_ids,
        )
    )

    state = load_state()

    current_inputs = {
        "focus":
            build_focus_fingerprint(
                all_focus_blocks
            ),

        "tasks":
            build_task_fingerprint(
                tasks
            ),

        "allocations":
            build_allocation_fingerprint(
                allocations,
                tasks_by_id,
            ),
    }

    force_rebuild = (
        os.environ
        .get(
            "FORCE_REBUILD",
            "",
        )
        .lower()
        == "true"
    )

    previous_inputs = {
        "focus":
            state.get("focus"),

        "tasks":
            state.get("tasks"),

        "allocations":
            state.get("allocations"),
    }

    changed = (
        current_inputs
        != previous_inputs
    )

    reconsider_requested = (
        schedule_status[
            "reconsider_requested"
        ]
    )

    if (
        not force_rebuild
        and not changed
        and not reconsider_requested
    ):
        print(
            "No relevant changes detected."
        )

        print(
            "Scheduler finished without "
            "rebuilding the schedule."
        )

        return

    if reconsider_requested:

        print(
            "Reconsider request detected."
        )

    elif force_rebuild:

        print(
            "Forced rebuild requested."
        )

    else:

        print(
            "Relevant scheduling changes "
            "detected."
        )

    # There is intentionally no active-Focus-Time
    # protection here. If the inputs change,
    # the scheduler rebuilds the remaining
    # schedule immediately.

    rebuild(
        tasks,
        all_focus_blocks,
        focus_blocks,
        allocations,
    )

    save_state(
        current_inputs
    )

    if reconsider_requested:

        clear_reconsider_request(
            schedule_status["page_id"],
            schedule_status[
                "reconsider_property"
            ],
        )

    print()
    print(
        "Scheduler finished successfully."
    )


if __name__ == "__main__":
    main()
One thing I deliberately kept simple

The source databases' Completed checkbox remains authoritative. The scheduler reads it, but does not automatically check it just because the allocations add up to the workload. That avoids the scheduler silently deciding that you've finished a task when you may actually want to review it first.

The (final) mechanism is separate: that's purely a scheduler-generated visual signal for "this is currently the last scheduled piece of this task." If you increase the workload, the scheduler can therefore move that signal to the newly created final allocation—even if the previous allocation is already completed.

Sponsored options
Ad
