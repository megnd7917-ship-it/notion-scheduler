import os
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

PFS_TASK_NAME = "PFS weekly hours"
PFS_WEEKLY_TARGET_MINUTES = 30 * 60


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
    """
    Human-readable time display.

    Examples:
        45 -> "45 minutes"
        60 -> "1 hour"
        90 -> "1 hour 30 minutes"
        120 -> "2 hours"
    """

    minutes = int(round(minutes))

    hours = minutes // 60
    remainder = minutes % 60

    parts = []

    if hours:
        if hours == 1:
            parts.append("1 hour")
        else:
            parts.append(f"{hours} hours")

    if remainder:
        parts.append(
            f"{remainder} minute"
            + ("" if remainder == 1 else "s")
        )

    if not parts:
        return "0 minutes"

    return " ".join(parts)


def format_allocation(amount_minutes, unit):
    """
    Display an allocation in a natural, readable form.

    Time-based units are displayed as actual time.
    Workload units remain in their natural units.
    """

    if unit in ("Hours", "Minutes"):
        return format_minutes(amount_minutes)

    amount = minutes_to_units(
        amount_minutes,
        unit,
    )

    if amount == int(amount):
        amount_text = str(int(amount))
    else:
        amount_text = f"{amount:g}"

    return (
        f"{amount_text} {unit}"
    )


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

        results.extend(
            data.get("results", [])
        )

        if not data.get("has_more"):
            break

        cursor = data.get("next_cursor")

    return results


def find_database(name):
    """
    Find the actual Notion database whose title exactly
    matches the requested name.

    Notion search may return data_source objects belonging
    to linked database views, so we verify the parent
    database's actual title before accepting it.
    """

    for obj in search_all(name):

        if obj.get("object") == "database":

            title = "".join(
                item.get("plain_text", "")
                for item in obj.get("title", [])
            ).strip()

            if title == name:
                return obj["id"]

        elif obj.get("object") == "data_source":

            parent = obj.get("parent", {})
            database_id = parent.get(
                "database_id"
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

    sources = data.get(
        "data_sources",
        [],
    )

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
            break

        cursor = data.get("next_cursor")

    return pages


# ============================================================
# PROPERTY HELPERS
# ============================================================

def title_value(page, property_name):
    prop = page["properties"].get(
        property_name
    )

    if not prop or prop["type"] != "title":
        return ""

    return "".join(
        item.get("plain_text", "")
        for item in prop.get("title", [])
    )


def checkbox_value(page, property_name):
    prop = page["properties"].get(
        property_name
    )

    if not prop or prop["type"] != "checkbox":
        return False

    return prop.get(
        "checkbox",
        False,
    )


def number_value(page, property_name):
    prop = page["properties"].get(
        property_name
    )

    if not prop or prop["type"] != "number":
        return None

    return prop.get("number")


def select_value(page, property_name):
    prop = page["properties"].get(
        property_name
    )

    if not prop or prop["type"] != "select":
        return None

    option = prop.get("select")

    if not option:
        return None

    return option.get("name")


def date_value(page, property_name):
    prop = page["properties"].get(
        property_name
    )

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
        "end": (
            parse_datetime(end)
            if end
            else None
        ),
    }


def relation_ids(page, property_name):
    prop = page["properties"].get(
        property_name
    )

    if not prop or prop["type"] != "relation":
        return []

    return [
        item["id"]
        for item in prop.get(
            "relation",
            [],
        )
    ]


def parse_datetime(value):
    if value.endswith("Z"):
        value = (
            value[:-1]
            + "+00:00"
        )

    dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=TZ
        )

    return dt.astimezone(TZ)


# ============================================================
# MASTER TO-DO LIST
# ============================================================

def read_master_tasks():

    database_id = find_database(
        "Master To-Do List"
    )

    data_source_id = get_data_source(
        database_id
    )

    pages = query_data_source(
        data_source_id
    )

    tasks = []

    for page in pages:

        if checkbox_value(
            page,
            "Completed",
        ):
            continue

        name = title_value(
            page,
            "Task",
        ).strip()

        if not name:
            continue

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
            "project": select_value(
                page,
                "Project",
            ),
            "deadline": date_value(
                page,
                "Deadline",
            ),
            "workload": workload,
            "unit": unit,
            "priority": select_value(
                page,
                "Priority level",
            ),
            "continuous": checkbox_value(
                page,
                "Continuous",
            ),
            "minutes": workload_to_minutes(
                workload,
                unit,
            ),
        })

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

        if not date:
            continue

        start = date["start"]
        end = date["end"]

        if not end:
            continue

        all_blocks.append({
            "page_id": page["id"],
            "start": start,
            "end": end,
        })

    all_blocks.sort(
        key=lambda block:
        block["start"]
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

    return all_blocks, schedulable_blocks


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

        allocations.append({
            "page_id": page["id"],
            "name": title_value(
                page,
                "Name",
            ),
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


# ============================================================
# EXISTING WORK
# ============================================================

def allocation_minutes(
    allocation,
    master_tasks_by_id,
):
    total = 0

    for master_id in allocation[
        "master_ids"
    ]:

        task = master_tasks_by_id.get(
            master_id
        )

        if not task:
            continue

        total += workload_to_minutes(
            allocation["allocation"],
            task["unit"],
        )

    return total


def calculate_existing_work(
    allocations,
    master_tasks_by_id,
):
    completed = {}
    planned = {}

    for allocation in allocations:

        minutes = allocation_minutes(
            allocation,
            master_tasks_by_id,
        )

        for master_id in allocation[
            "master_ids"
        ]:

            if allocation["completed"]:

                completed[master_id] = (
                    completed.get(
                        master_id,
                        0,
                    )
                    + minutes
                )

            else:

                planned[master_id] = (
                    planned.get(
                        master_id,
                        0,
                    )
                    + minutes
                )

    return completed, planned


def reserve_existing_focus_time(
    allocations,
    focus_blocks,
    master_tasks_by_id,
):
    blocks_by_id = {
        block["page_id"]: block
        for block in focus_blocks
    }

    for allocation in allocations:

        if allocation["completed"]:
            continue

        focus_ids = allocation[
            "focus_ids"
        ]

        if not focus_ids:
            continue

        block = blocks_by_id.get(
            focus_ids[0]
        )

        if not block:
            continue

        minutes = allocation_minutes(
            allocation,
            master_tasks_by_id,
        )

        block["remaining"] = max(
            0,
            block["remaining"] - minutes,
        )


# ============================================================
# PRIORITY
# ============================================================

def priority_multiplier(priority):

    if priority == "High":
        return 1.20

    if priority == "Medium":
        return 1.05

    return 1.00


def hours_until_deadline(
    task,
    now,
):
    if not task["deadline"]:
        return None

    return (
        task["deadline"]["start"]
        - now
    ).total_seconds() / 3600


def task_score(
    task,
    remaining_minutes,
    now,
):
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
            100
            / ((hours + 1) ** 2)
        )

    priority_score = (
        priority_multiplier(
            task["priority"]
        )
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
# ELIGIBILITY
# ============================================================

def task_is_eligible(
    task,
    remaining_minutes,
    now,
):
    if remaining_minutes <= 0:
        return False

    deadline = task["deadline"]

    if not deadline:
        return True

    deadline_dt = deadline["start"]
    deadline_date = deadline_dt.date()
    today = now.date()

    if deadline_dt <= now:
        return True

    # On deadline day, only schedule if
    # 15 minutes or less remain.
    if deadline_date == today:
        return remaining_minutes <= 15

    return True


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

    # Continuous tasks must fit entirely
    # inside one Focus Time block.
    if task["continuous"]:

        if remaining_minutes <= available_minutes:
            return remaining_minutes

        return 0

    # Finish short tasks.
    if maximum <= PREFERRED_MAX_CHUNK_MINUTES:
        return maximum

    chunk = PREFERRED_MAX_CHUNK_MINUTES

    remainder = (
        remaining_minutes
        - chunk
    )

    # Avoid a tiny final fragment.
    if (
        0 < remainder
        <= MIN_CHUNK_MINUTES
    ):
        return remaining_minutes

    return chunk


# ============================================================
# GENERAL SCHEDULING
# ============================================================

def schedule_general_tasks(
    tasks,
    focus_blocks,
    completed,
    planned,
):
    now = datetime.now(TZ)

    remaining = {}

    for task in tasks:

        # PFS weekly-hours is handled separately.
        if task["task"] == PFS_TASK_NAME:
            continue

        task_id = task["page_id"]

        already_completed = (
            completed.get(
                task_id,
                0,
            )
        )

        already_planned = (
            planned.get(
                task_id,
                0,
            )
        )

        remaining[task_id] = max(
            0,
            task["minutes"]
            - already_completed
            - already_planned,
        )

    new_allocations = []

    for block in focus_blocks:

        while (
            block["remaining"]
            >= MIN_CHUNK_MINUTES
        ):

            candidates = []

            for task in tasks:

                if task["task"] == PFS_TASK_NAME:
                    continue

                task_id = task["page_id"]

                rem = remaining.get(
                    task_id,
                    0,
                )

                if rem <= 0:
                    continue

                if not task_is_eligible(
                    task,
                    rem,
                    now,
                ):
                    continue

                score = task_score(
                    task,
                    rem,
                    now,
                )

                # Slightly favor work that has not
                # already been scheduled earlier
                # on the same day.
                same_day_minutes = 0

                for allocation in new_allocations:

                    if (
                        allocation["task"]["page_id"]
                        != task_id
                    ):
                        continue

                    allocation_block = (
                        next(
                            (
                                b
                                for b in focus_blocks
                                if b["page_id"]
                                == allocation[
                                    "focus_page_id"
                                ]
                            ),
                            None,
                        )
                    )

                    if (
                        allocation_block
                        and
                        allocation_block[
                            "start"
                        ].date()
                        ==
                        block["start"].date()
                    ):
                        same_day_minutes += (
                            allocation[
                                "amount_minutes"
                            ]
                        )

                if same_day_minutes > 0:
                    score *= 0.85

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

            new_allocations.append({
                "task": chosen,
                "amount_minutes": amount,
                "focus_page_id": block[
                    "page_id"
                ],
            })

            block["remaining"] -= amount

            remaining[
                chosen["page_id"]
            ] -= amount

    return new_allocations, remaining


# ============================================================
# PFS WEEKLY TARGET
# ============================================================

def current_week_range():

    today = datetime.now(
        TZ
    ).date()

    monday = (
        today
        - timedelta(
            days=today.weekday()
        )
    )

    next_monday = (
        monday
        + timedelta(days=7)
    )

    return monday, next_monday


def pfs_minutes_this_week(
    allocations,
    all_focus_blocks,
    master_tasks_by_id,
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

        is_pfs = False
        pfs_unit = None

        for master_id in allocation[
            "master_ids"
        ]:

            task = master_tasks_by_id.get(
                master_id
            )

            if (
                task
                and task["project"] == "PFS"
            ):
                is_pfs = True
                pfs_unit = task["unit"]
                break

        if not is_pfs:
            continue

        for focus_id in allocation[
            "focus_ids"
        ]:

            block = blocks_by_id.get(
                focus_id
            )

            if not block:
                continue

            date = block["start"].date()

            if (
                week_start
                <= date
                < week_end
            ):
                total += workload_to_minutes(
                    allocation["allocation"],
                    pfs_unit,
                )

    return total


def schedule_pfs(
    pfs_task,
    focus_blocks,
    existing_allocations,
    all_focus_blocks,
    master_tasks_by_id,
):
    existing_minutes = (
        pfs_minutes_this_week(
            existing_allocations,
            all_focus_blocks,
            master_tasks_by_id,
        )
    )

    remaining_target = max(
        0,
        PFS_WEEKLY_TARGET_MINUTES
        - existing_minutes,
    )

    if remaining_target <= 0:
        return []

    week_start, week_end = (
        current_week_range()
    )

    new_allocations = []

    for block in focus_blocks:

        if block["remaining"] < MIN_CHUNK_MINUTES:
            continue

        date = block["start"].date()

        if date < week_start:
            continue

        if date >= week_end:
            continue

        amount = min(
            block["remaining"],
            remaining_target,
        )

        if amount < MIN_CHUNK_MINUTES:
            continue

        new_allocations.append({
            "task": pfs_task,
            "amount_minutes": amount,
            "focus_page_id": block[
                "page_id"
            ],
        })

        block["remaining"] -= amount

        remaining_target -= amount

        if remaining_target <= 0:
            break

    return new_allocations


# ============================================================
# CREATE TASK ALLOCATION
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

    amount_minutes = allocation[
        "amount_minutes"
    ]

    unit = task["unit"]

    display_amount = format_allocation(
        amount_minutes,
        unit,
    )

    name = (
        f'{task["task"]} — '
        f'{display_amount}'
    )

    # The Unit property remains the task's
    # underlying natural unit. The Name is the
    # human-readable display.
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

        "Master To-Do List": {
            "relation": [
                {
                    "id": task["page_id"]
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

        deadline = task["deadline"]["start"]

        if (
            deadline
            <= now + timedelta(days=18)
        ):
            required += rem

    difference = (
        available - required
    )

    if difference >= 0:

        return (
            "🟢 On track — enough time available "
            f"({format_minutes(difference)} surplus)"
        )

    return (
        "🟠 Needs attention — "
        f"{format_minutes(abs(difference))} "
        "additional Focus Time needed"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("========================================")
    print("          NOTION SCHEDULER")
    print("========================================")
    print()

    # --------------------------------------------------------
    # MASTER TASKS
    # --------------------------------------------------------

    print("Reading Master To-Do List...")

    tasks = read_master_tasks()

    print(
        f"Master tasks found: "
        f"{len(tasks)}"
    )

    # --------------------------------------------------------
    # FOCUS TIME
    # --------------------------------------------------------

    print("Reading Focus time...")

    all_focus_blocks, focus_blocks = (
        read_focus_time()
    )

    print(
        f"Focus Time blocks found: "
        f"{len(focus_blocks)}"
    )

    # --------------------------------------------------------
    # EXISTING ALLOCATIONS
    # --------------------------------------------------------

    print(
        "Reading existing Task Allocations..."
    )

    existing_allocations = (
        read_allocations()
    )

    print(
        f"Existing allocations found: "
        f"{len(existing_allocations)}"
    )

    master_tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    completed, planned = (
        calculate_existing_work(
            existing_allocations,
            master_tasks_by_id,
        )
    )

    reserve_existing_focus_time(
        existing_allocations,
        focus_blocks,
        master_tasks_by_id,
    )

    # --------------------------------------------------------
    # PFS
    # --------------------------------------------------------

    pfs_task = None

    for task in tasks:

        if task["task"] == PFS_TASK_NAME:
            pfs_task = task
            break

    pfs_allocations = []

    if pfs_task:

        print()
        print(
            "PFS weekly target: ACTIVE"
        )

        pfs_allocations = schedule_pfs(
            pfs_task,
            focus_blocks,
            existing_allocations,
            all_focus_blocks,
            master_tasks_by_id,
        )

        print(
            f"PFS allocations to create: "
            f"{len(pfs_allocations)}"
        )

    else:

        print()
        print(
            "PFS weekly target: INACTIVE"
        )

    # --------------------------------------------------------
    # GENERAL TASKS
    # --------------------------------------------------------

    general_allocations, remaining = (
        schedule_general_tasks(
            tasks,
            focus_blocks,
            completed,
            planned,
        )
    )

    all_new_allocations = (
        general_allocations
        + pfs_allocations
    )

    # --------------------------------------------------------
    # CREATE ALLOCATIONS
    # --------------------------------------------------------

    print()
    print(
        f"New allocations to create: "
        f"{len(all_new_allocations)}"
    )

    for allocation in all_new_allocations:

        create_allocation(
            allocation
        )

        task = allocation["task"]

        display_amount = format_allocation(
            allocation["amount_minutes"],
            task["unit"],
        )

        print(
            f'  {task["task"]} → '
            f'{display_amount}'
        )

    # --------------------------------------------------------
    # STATUS
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
    print(
        "Scheduler finished successfully."
    )


if __name__ == "__main__":
    main()
