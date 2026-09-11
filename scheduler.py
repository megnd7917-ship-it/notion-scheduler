import os
from datetime import datetime, timedelta, time
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

PFS_TASK_NAME = "PFS weekly hours"
PFS_WEEKLY_TARGET_MINUTES = 30 * 60


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

    response = requests.request(
        method,
        url,
        headers=HEADERS,
        **kwargs,
    )

    if not response.ok:
        raise RuntimeError(
            f"Notion API error {response.status_code}: "
            f"{response.text}"
        )

    return response.json()


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

        results.extend(data.get("results", []))

        if not data.get("has_more"):
            break

        cursor = data.get("next_cursor")

    return results


def find_database(name):
    for obj in search_all(name):

        if obj.get("object") == "data_source":
            parent = obj.get("parent", {})
            database_id = parent.get("database_id")

            if database_id:
                return database_id

        elif obj.get("object") == "database":
            title = ""

            for item in obj.get("title", []):
                if item.get("plain_text"):
                    title += item["plain_text"]

            if title.strip() == name:
                return obj["id"]

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

        pages.extend(data.get("results", []))

        if not data.get("has_more"):
            break

        cursor = data.get("next_cursor")

    return pages


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

    if not option:
        return None

    return option.get("name")


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


# ============================================================
# WORKLOAD CONVERSIONS
# ============================================================

MINUTES_PER_UNIT = {
    "Pages": 5,
    "LSAT Questions": 3,
    "Papers": 10,
    "Hours": 60,
    "Minutes": 1,
}


def workload_minutes(workload, unit):
    if workload is None or not unit:
        return 0

    if unit not in MINUTES_PER_UNIT:
        raise RuntimeError(
            f'Unknown workload unit "{unit}".'
        )

    return workload * MINUTES_PER_UNIT[unit]


# ============================================================
# SMALL TO-DO LISTS
# ============================================================

SOURCE_DATABASES = {
    "ENL 248 To-Do": "ENL 248",
    "COM 210 To-Do": "COM 210",
    "LSAT To-Do": "LSAT",
    "Independent Study To-Do": "Independent Study",
    "JST To-Do": "JST",
    "Reader To-Do": "Reader",
    "PFS To-Do": "PFS",
    "Personal To-Do": "Personal",
    "Applications To-Do": "Applications",
}


def read_small_tasks():
    tasks = []

    for database_name, project in SOURCE_DATABASES.items():

        database_id = find_database(database_name)
        data_source_id = get_data_source(database_id)

        pages = query_data_source(data_source_id)

        for page in pages:

            if not checkbox_value(page, "Ready"):
                continue

            task_name = title_value(page, "Task")

            if not task_name:
                continue

            tasks.append({
                "source_page_id": page["id"],
                "source_database": database_name,
                "task": task_name,
                "project": project,
                "deadline": date_value(page, "Deadline"),
                "workload": number_value(page, "Workload"),
                "unit": select_value(page, "Unit"),
                "priority": select_value(page, "Priority"),
                "continuous": checkbox_value(page, "Continuous"),
            })

    for task in tasks:
        task["minutes"] = workload_minutes(
            task["workload"],
            task["unit"],
        )

    return tasks


# ============================================================
# MASTER TO-DO LIST
# ============================================================

def read_master_tasks():
    database_id = find_database("Master To-Do List")
    data_source_id = get_data_source(database_id)

    pages = query_data_source(data_source_id)

    tasks = []

    for page in pages:

        if checkbox_value(page, "Completed"):
            continue

        task = {
            "page_id": page["id"],
            "task": title_value(page, "Task"),
            "project": select_value(page, "Project"),
            "deadline": date_value(page, "Deadline"),
            "workload": number_value(page, "Workload"),
            "unit": select_value(page, "Unit"),
            "priority": select_value(page, "Priority level"),
            "continuous": checkbox_value(page, "Continuous"),
        }

        task["minutes"] = workload_minutes(
            task["workload"],
            task["unit"],
        )

        tasks.append(task)

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

    blocks = []

    for page in pages:

        date = date_value(page, "Date")

        if not date:
            continue

        start = date["start"]
        end = date["end"]

        if not end:
            continue

        if end <= now:
            continue

        if start >= horizon:
            continue

        if start < now:
            start = now

        if end > horizon:
            end = horizon

        minutes = int(
            (end - start).total_seconds() / 60
        )

        if minutes <= 0:
            continue

        blocks.append({
            "page_id": page["id"],
            "start": start,
            "end": end,
            "remaining": minutes,
        })

    blocks.sort(
        key=lambda block: block["start"]
    )

    return blocks


# ============================================================
# EXISTING TASK ALLOCATIONS
# ============================================================

def read_allocations():
    database_id = find_database("Task Allocations")
    data_source_id = get_data_source(database_id)

    pages = query_data_source(data_source_id)

    allocations = []

    for page in pages:

        allocations.append({
            "page_id": page["id"],
            "name": title_value(page, "Name"),
            "focus_ids": relation_ids(
                page,
                "Focus time",
            ),
            "master_ids": relation_ids(
                page,
                "Master To-Do List",
            ),
            "allocation": number_value(
                page,
                "Allocation",
            ) or 0,
            "completed": checkbox_value(
                page,
                "Completion",
            ),
        })

    return allocations


def completed_minutes_by_task(allocations):
    completed = {}

    for allocation in allocations:

        if not allocation["completed"]:
            continue

        for task_id in allocation["master_ids"]:

            completed[task_id] = (
                completed.get(task_id, 0)
                + allocation["allocation"]
            )

    return completed


# ============================================================
# PFS WEEKLY TARGET
# ============================================================

def monday_of_week(date):
    return date - timedelta(
        days=date.weekday()
    )


def pfs_week_start():
    return monday_of_week(
        datetime.now(TZ).date()
    )


def pfs_allocated_minutes_this_week(
    allocations,
    master_tasks_by_id,
):
    week_start = pfs_week_start()
    week_end = week_start + timedelta(days=7)

    total = 0

    for allocation in allocations:

        for master_id in allocation["master_ids"]:

            task = master_tasks_by_id.get(master_id)

            if not task:
                continue

            if task["project"] != "PFS":
                continue

            for focus_id in allocation["focus_ids"]:
                # Actual Focus Time dates are handled separately.
                # This function is replaced by the dated version below.
                pass

    return total


def pfs_minutes_from_allocations(
    allocations,
    focus_blocks_by_id,
    master_tasks_by_id,
):
    week_start = pfs_week_start()
    week_end = week_start + timedelta(days=7)

    total = 0

    for allocation in allocations:

        for master_id in allocation["master_ids"]:

            task = master_tasks_by_id.get(master_id)

            if not task:
                continue

            if task["project"] != "PFS":
                continue

            for focus_id in allocation["focus_ids"]:

                block = focus_blocks_by_id.get(
                    focus_id
                )

                if not block:
                    continue

                date = block["start"].date()

                if week_start <= date < week_end:
                    total += allocation["allocation"]

    return total


# ============================================================
# URGENCY
# ============================================================

def priority_multiplier(priority):
    if priority == "High":
        return 1.20

    if priority == "Medium":
        return 1.05

    return 1.0


def hours_until_deadline(task, now):
    if not task["deadline"]:
        return None

    return (
        task["deadline"]["start"] - now
    ).total_seconds() / 3600


def task_score(task, remaining_minutes, now):
    hours = hours_until_deadline(
        task,
        now,
    )

    if hours is None:
        deadline_score = 0.1

    elif hours <= 0:
        deadline_score = 100000

    else:
        deadline_score = (
            100 / ((hours + 1) ** 2)
        )

    priority_score = priority_multiplier(
        task["priority"]
    )

    size_score = min(
        2.0,
        remaining_minutes / 120,
    )

    return (
        deadline_score
        * priority_score
        + size_score
    )


# ============================================================
# ALLOCATION SIZE
# ============================================================

def choose_allocation_size(
    task,
    remaining_minutes,
    available_minutes,
):
    maximum = min(
        remaining_minutes,
        available_minutes,
    )

    if maximum <= 0:
        return 0

    # Continuous tasks must fit entirely.
    if task["continuous"]:

        if remaining_minutes <= available_minutes:
            return remaining_minutes

        return 0

    # Finish small remaining amounts.
    if maximum <= PREFERRED_MAX_CHUNK_MINUTES:
        return maximum

    # Prefer 45-minute chunks.
    chunk = PREFERRED_MAX_CHUNK_MINUTES

    remainder = remaining_minutes - chunk

    # Avoid creating a tiny final fragment.
    if 0 < remainder < MIN_CHUNK_MINUTES:
        return remaining_minutes

    return chunk


# ============================================================
# GENERAL TASK SCHEDULING
# ============================================================

def schedule_general_tasks(
    tasks,
    focus_blocks,
    completed_minutes,
):
    now = datetime.now(TZ)

    remaining = {}

    for task in tasks:

        already_done = completed_minutes.get(
            task["page_id"],
            0,
        )

        remaining[task["page_id"]] = max(
            0,
            task["minutes"] - already_done,
        )

    allocations = []

    for block in focus_blocks:

        while block["remaining"] >= MIN_CHUNK_MINUTES:

            candidates = []

            for task in tasks:

                rem = remaining[
                    task["page_id"]
                ]

                if rem <= 0:
                    continue

                # PFS weekly-hours is handled separately.
                if (
                    task["project"] == "PFS"
                    and task["task"] == PFS_TASK_NAME
                ):
                    continue

                score = task_score(
                    task,
                    rem,
                    now,
                )

                candidates.append(
                    (score, task)
                )

            if not candidates:
                break

            candidates.sort(
                key=lambda item: item[0],
                reverse=True,
            )

            chosen = None
            amount = 0

            for _, task in candidates:

                amount = choose_allocation_size(
                    task,
                    remaining[
                        task["page_id"]
                    ],
                    block["remaining"],
                )

                if amount > 0:
                    chosen = task
                    break

            if chosen is None:
                break

            allocations.append({
                "task": chosen,
                "amount": amount,
                "focus_page_id": block["page_id"],
            })

            block["remaining"] -= amount

            remaining[
                chosen["page_id"]
            ] -= amount

    return allocations, remaining


# ============================================================
# PFS SCHEDULING
# ============================================================

def schedule_pfs(
    pfs_task,
    focus_blocks,
    existing_allocations,
    master_tasks_by_id,
):
    """
    PFS weekly hours is a Monday-Sunday target.

    Individual PFS tasks count toward the weekly target.
    Remaining target time is filled with the PFS weekly-hours
    allocation.

    PFS receives a slight preference, but does not displace
    tasks with significantly more urgent deadlines.
    """

    week_start = pfs_week_start()
    week_end = week_start + timedelta(days=7)

    focus_blocks_by_id = {
        block["page_id"]: block
        for block in focus_blocks
    }

    already_allocated = (
        pfs_minutes_from_allocations(
            existing_allocations,
            focus_blocks_by_id,
            master_tasks_by_id,
        )
    )

    remaining_target = max(
        0,
        PFS_WEEKLY_TARGET_MINUTES
        - already_allocated,
    )

    if remaining_target <= 0:
        return []

    allocations = []

    for block in focus_blocks:

        if block["remaining"] <= 0:
            continue

        if block["start"].date() < week_start:
            continue

        if block["start"].date() >= week_end:
            continue

        amount = min(
            block["remaining"],
            remaining_target,
        )

        if amount < MIN_CHUNK_MINUTES:
            continue

        allocations.append({
            "task": pfs_task,
            "amount": amount,
            "focus_page_id": block["page_id"],
        })

        block["remaining"] -= amount
        remaining_target -= amount

        if remaining_target <= 0:
            break

    return allocations


# ============================================================
# CREATE ALLOCATION
# ============================================================

def create_allocation(
    allocation,
    master_page_id,
):
    database_id = find_database(
        "Task Allocations"
    )

    data_source_id = get_data_source(
        database_id
    )

    task = allocation["task"]
    amount = allocation["amount"]

    unit = task["unit"]

    if unit == "Hours":
        display_amount = (
            f"{amount / 60:g} hours"
        )
    elif unit:
        display_amount = (
            f"{amount:g} {unit}"
        )
    else:
        display_amount = task["task"]

    name = (
        f'{task["task"]} — '
        f'{display_amount}'
    )

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
        "Allocation": {
            "number": amount
        },
        "Completion": {
            "checkbox": False
        },
        "Master To-Do List": {
            "relation": [
                {
                    "id": master_page_id
                }
            ]
        },
        "Focus time": {
            "relation": [
                {
                    "id": allocation["focus_page_id"]
                }
            ]
        },
    }

    return notion(
        "POST",
        "pages",
        json={
            "parent": {
                "data_source_id": data_source_id
            },
            "properties": properties,
        },
    )


# ============================================================
# STATUS
# ============================================================

def calculate_status(
    tasks,
    remaining,
    focus_blocks,
):
    now = datetime.now(TZ)

    available = sum(
        block["remaining"]
        for block in focus_blocks
    )

    required = 0

    for task in tasks:

        rem = remaining.get(
            task["page_id"],
            0,
        )

        if rem <= 0:
            continue

        if not task["deadline"]:
            continue

        if (
            task["deadline"]["start"]
            <= now + timedelta(days=18)
        ):
            required += rem

    difference = available - required

    if difference >= 0:

        hours = difference // 60
        minutes = difference % 60

        return (
            f"🟢 On track — enough time available "
            f"({hours}h {minutes}m surplus)"
        )

    shortfall = abs(difference)

    hours = shortfall // 60
    minutes = shortfall % 60

    return (
        f"🟠 Needs attention — "
        f"{hours}h {minutes}m "
        f"additional Focus Time needed"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("========================================")
    print("        NOTION SCHEDULER")
    print("========================================")
    print()

    print("Reading Ready tasks...")
    small_tasks = read_small_tasks()

    print(
        f"Ready tasks found: {len(small_tasks)}"
    )

    print("Reading Master To-Do List...")
    master_tasks = read_master_tasks()

    print(
        f"Master tasks found: {len(master_tasks)}"
    )

    # Match Master tasks to Ready tasks.
    ready_keys = {
        (
            task["task"],
            task["project"],
        )
        for task in small_tasks
    }

    tasks = [
        task
        for task in master_tasks
        if (
            task["task"],
            task["project"],
        ) in ready_keys
    ]

    print(
        f"Schedulable tasks: {len(tasks)}"
    )

    print("Reading Focus time...")
    focus_blocks = read_focus_time()

    print(
        f"Focus Time blocks: {len(focus_blocks)}"
    )

    print("Reading existing allocations...")
    existing_allocations = read_allocations()

    completed_minutes = (
        completed_minutes_by_task(
            existing_allocations
        )
    )

    master_tasks_by_id = {
        task["page_id"]: task
        for task in master_tasks
    }

    # --------------------------------------------------------
    # PFS
    # --------------------------------------------------------

    pfs_task = None

    for task in tasks:

        if (
            task["project"] == "PFS"
            and task["task"] == PFS_TASK_NAME
        ):
            pfs_task = task
            break

    pfs_allocations = []

    if pfs_task:

        print()
        print("PFS weekly target: ACTIVE")

        pfs_allocations = schedule_pfs(
            pfs_task,
            focus_blocks,
            existing_allocations,
            master_tasks_by_id,
        )

        print(
            f"PFS target allocations: "
            f"{len(pfs_allocations)}"
        )

    else:

        print()
        print("PFS weekly target: INACTIVE")

    # --------------------------------------------------------
    # General tasks
    # --------------------------------------------------------

    general_tasks = [
        task
        for task in tasks
        if not (
            task["project"] == "PFS"
            and task["task"] == PFS_TASK_NAME
        )
    ]

    general_allocations, remaining = (
        schedule_general_tasks(
            general_tasks,
            focus_blocks,
            completed_minutes,
        )
    )

    all_new_allocations = (
        general_allocations
        + pfs_allocations
    )

    # --------------------------------------------------------
    # Write allocations
    # --------------------------------------------------------

    print()
    print(
        f"New allocations: "
        f"{len(all_new_allocations)}"
    )

    for allocation in all_new_allocations:

        create_allocation(
            allocation,
            allocation["task"]["page_id"],
        )

        print(
            f"  {allocation['task']['task']} "
            f"→ {allocation['amount']} "
            f"{allocation['task']['unit']}"
        )

    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------

    status = calculate_status(
        tasks,
        remaining,
        focus_blocks,
    )

    print()
    print("----------------------------------------")
    print(status)
    print("----------------------------------------")
    print()
    print("Scheduler finished successfully.")


if __name__ == "__main__":
    main()
