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


def parse_datetime(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ)


# ============================================================
# MASTER TO-DO LIST
# ============================================================

def read_master_tasks():
    database_id = find_database("Master To-Do List")
    data_source_id = get_data_source(database_id)
    pages = query_data_source(data_source_id)

    tasks = []

    for page in pages:
        name = title_value(page, "Task").strip()
        if not name:
            continue

        tasks.append({
            "page_id": page["id"],
            "task": name,
            "project": select_value(page, "Project"),
            "deadline": date_value(page, "Deadline"),
            "workload": number_value(page, "Workload"),
            "unit": select_value(page, "Unit"),
            "priority": select_value(page, "Priority level"),
            "continuous": checkbox_value(page, "Continuous"),
            "completed": checkbox_value(page, "Completed"),
            "minutes": workload_to_minutes(
                number_value(page, "Workload"),
                select_value(page, "Unit"),
            ),
        })

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
            "master_ids": relation_ids(page, "Master To-Do List"),
            "allocation": number_value(page, "Allocation") or 0,
            "completed": checkbox_value(page, "Completion"),
        })

    return allocations


# ============================================================
# ALLOCATION ACCOUNTING
# ============================================================

def allocation_minutes(allocation, master_tasks_by_id):
    total = 0

    for master_id in allocation["master_ids"]:
        task = master_tasks_by_id.get(master_id)
        if not task:
            continue

        total += workload_to_minutes(
            allocation["allocation"],
            task["unit"],
        )

    return total


def calculate_completed_work(allocations, master_tasks_by_id):
    completed = {}

    for allocation in allocations:
        if not allocation["completed"]:
            continue

        minutes = allocation_minutes(
            allocation,
            master_tasks_by_id,
        )

        for master_id in allocation["master_ids"]:
            completed[master_id] = (
                completed.get(master_id, 0) + minutes
            )

    return completed


def pfs_minutes_this_week(
    allocations,
    all_focus_blocks,
    master_tasks_by_id,
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

        for master_id in allocation["master_ids"]:
            task = master_tasks_by_id.get(master_id)
            if task and task["project"] == PFS_PROJECT_NAME:
                is_pfs = True
                pfs_unit = task["unit"]
                break

        if not is_pfs:
            continue

        # The synthetic PFS weekly-hours task uses Hours.
        if pfs_unit not in MINUTES_PER_UNIT:
            pfs_unit = "Hours"

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


def current_week_range():
    today = datetime.now(TZ).date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=7)


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
    return stable_hash([
        (
            block["page_id"],
            block["start"].isoformat(),
            block["end"].isoformat(),
            block.get("last_edited_time"),
        )
        for block in all_focus_blocks
        if block["start"] < datetime.now(TZ) + timedelta(days=PLANNING_DAYS)
    ])


def build_master_fingerprint(tasks):
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
        )
        for task in sorted(tasks, key=lambda t: t["page_id"])
    ])


def build_completed_allocation_fingerprint(allocations):
    return stable_hash(sorted(
        (
            allocation["page_id"],
            tuple(sorted(allocation["master_ids"])),
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


def hours_until_deadline(task, now):
    if not task["deadline"]:
        return None

    return (
        task["deadline"]["start"] - now
    ).total_seconds() / 3600


def task_score(task, remaining_minutes, now, same_day_minutes):
    hours = hours_until_deadline(task, now)

    if hours is None:
        deadline_score = 0.1
    elif hours <= 0:
        # Overdue tasks are handled as absolute-priority candidates.
        deadline_score = 100000000
    else:
        deadline_score = 100 / ((hours + 1) ** 2)

    score = (
        deadline_score
        * priority_multiplier(task["priority"])
        + min(2.0, remaining_minutes / 120)
    )

    if same_day_minutes > 0:
        score *= 0.85

    return score


def task_is_eligible(task, remaining_minutes, block_start, block_end, now):
    if remaining_minutes <= 0:
        return False

    deadline = task["deadline"]

    # No deadline: always eligible.
    if not deadline:
        return True

    deadline_dt = deadline["start"]

    # Overdue: absolute priority and eligible immediately.
    if deadline_dt <= now:
        return True

    # A task due today should only be left for deadline-day
    # scheduling when it has <=15 minutes remaining.
    if deadline_dt.date() == now.date():
        if remaining_minutes > MIN_CHUNK_MINUTES:
            return False

    # Hard deadline barrier: ordinary work cannot extend past
    # the deadline.
    if block_start >= deadline_dt:
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

    # Hard deadline barrier for non-overdue tasks.
    if task["deadline"] and task["deadline"]["start"] > now:
        deadline = task["deadline"]["start"]
        deadline_capacity = int(
            (deadline - block_start).total_seconds() / 60
        )
        maximum = min(maximum, deadline_capacity)

    if maximum <= 0:
        return 0

    if task["continuous"]:
        if remaining_minutes <= maximum:
            return remaining_minutes
        return 0

    # Finish short tasks rather than fragmenting them.
    if maximum <= PREFERRED_MAX_CHUNK_MINUTES:
        return maximum

    chunk = PREFERRED_MAX_CHUNK_MINUTES
    remainder = remaining_minutes - chunk

    # Avoid a useless 1–14 minute final fragment.
    if 0 < remainder <= MIN_CHUNK_MINUTES:
        return remaining_minutes

    return chunk


# ============================================================
# PFS SCHEDULING
# ============================================================

def pfs_score(block, remaining_target, week_end):
    """
    PFS has only a slight preference. Its pressure increases
    as the week progresses, because the 30-hour minimum still
    has to be reached by Sunday night.

    Weekdays receive a modest preference over weekends.
    """
    block_date = block["start"].date()
    is_weekday = block_date.weekday() < 5

    today = datetime.now(TZ).date()
    days_left = max(1, (week_end - block_date).days)

    # Base preference is deliberately modest.
    score = 0.35 if is_weekday else 0.20

    # Increase pressure as the week closes.
    score += min(2.0, 7.0 / days_left)

    # A large remaining deficit should matter, but not enough
    # to override a genuinely urgent overdue task.
    deficit_hours = remaining_target / 60
    score += min(2.5, deficit_hours / 12)

    return score


def schedule_pfs_candidates(
    pfs_task,
    focus_blocks,
    remaining_target,
    general_remaining,
    new_allocations,
    now,
):
    if remaining_target <= 0:
        return remaining_target

    week_start, week_end = current_week_range()

    for block in focus_blocks:
        if remaining_target < MIN_CHUNK_MINUTES:
            break

        date = block["start"].date()
        if date < week_start or date >= week_end:
            continue

        if block["remaining"] < MIN_CHUNK_MINUTES:
            continue

        # PFS is competing with ordinary tasks. Only use this
        # function for a block after ordinary scheduling has had
        # the opportunity to place an urgent task.
        amount = min(
            block["remaining"],
            remaining_target,
            PREFERRED_MAX_CHUNK_MINUTES,
        )

        if amount < MIN_CHUNK_MINUTES:
            continue

        new_allocations.append({
            "task": pfs_task,
            "amount_minutes": amount,
            "focus_page_id": block["page_id"],
        })

        block["remaining"] -= amount
        remaining_target -= amount

    return remaining_target


# ============================================================
# GENERAL + PFS COMPETITIVE SCHEDULING
# ============================================================

def schedule_tasks(
    tasks,
    focus_blocks,
    completed,
    pfs_task,
):
    now = datetime.now(TZ)

    remaining = {}

    for task in tasks:
        if task["completed"]:
            continue

        # PFS weekly-hours is not a normal workload task.
        if task["task"] == PFS_TASK_NAME:
            continue

        task_id = task["page_id"]
        already_completed = completed.get(task_id, 0)

        remaining[task_id] = max(
            0,
            task["minutes"] - already_completed,
        )

    new_allocations = []

    # The weekly PFS deficit is based on completed PFS work.
    # At this point all incomplete old allocations have been
    # removed, so there is no double-counting of provisional work.
    if pfs_task:
        # Caller provides this value through the task itself.
        pass

    week_start, week_end = current_week_range()

    # We need the PFS target calculated before entering the block loop.
    # Completed PFS minutes are stored by the caller as a special value.
    pfs_completed_minutes = getattr(
        schedule_tasks,
        "_pfs_completed_minutes",
        0,
    )
    pfs_remaining_target = max(
        0,
        PFS_WEEKLY_TARGET_MINUTES - pfs_completed_minutes,
    )

    for block in focus_blocks:
        while block["remaining"] >= MIN_CHUNK_MINUTES:
            candidates = []

            # Ordinary tasks.
            for task in tasks:
                if task["task"] == PFS_TASK_NAME:
                    continue
                if task["completed"]:
                    continue

                task_id = task["page_id"]
                rem = remaining.get(task_id, 0)

                if rem <= 0:
                    continue

                if not task_is_eligible(
                    task,
                    rem,
                    block["start"],
                    block["end"],
                    now,
                ):
                    continue

                same_day_minutes = sum(
                    allocation["amount_minutes"]
                    for allocation in new_allocations
                    if allocation["task"]["page_id"] == task_id
                    and next(
                        (
                            b for b in focus_blocks
                            if b["page_id"] == allocation["focus_page_id"]
                        ),
                        {"start": None},
                    )["start"] is not None
                    and next(
                        (
                            b for b in focus_blocks
                            if b["page_id"] == allocation["focus_page_id"]
                        ),
                        {"start": None},
                    )["start"].date() == block["start"].date()
                )

                score = task_score(
                    task,
                    rem,
                    now,
                    same_day_minutes,
                )

                candidates.append(("general", score, task))

            # PFS candidate.
            if (
                pfs_task
                and pfs_remaining_target >= MIN_CHUNK_MINUTES
                and block["start"].date() < week_end
                and block["start"].date() >= week_start
            ):
                candidates.append((
                    "pfs",
                    pfs_score(
                        block,
                        pfs_remaining_target,
                        week_end,
                    ),
                    pfs_task,
                ))

            if not candidates:
                break

            # Absolute overdue tasks always beat everything else.
            overdue = [
                candidate
                for candidate in candidates
                if (
                    candidate[0] == "general"
                    and candidate[2]["deadline"]
                    and candidate[2]["deadline"]["start"] <= now
                )
            ]

            if overdue:
                candidates = overdue

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
                # Remove this task from consideration for this
                # block rather than getting stuck in a loop.
                remaining[chosen["page_id"]] = 0
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

    # Synthetic PFS weekly-hours allocations are always Hours.
    unit = task["unit"]
    if task["task"] == PFS_TASK_NAME:
        unit = "Hours"

    display_amount = format_allocation(
        amount_minutes,
        unit,
    )

    name = f'{task["task"]} — {display_amount}'

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
        "Master To-Do List": {
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

def calculate_status(tasks, remaining, focus_blocks):
    now = datetime.now(TZ)

    available = sum(
        block["remaining"]
        for block in focus_blocks
    )

    required = 0

    for task in tasks:
        rem = remaining.get(task["page_id"], 0)

        if rem <= 0 or not task["deadline"]:
            continue

        deadline = task["deadline"]["start"]

        if deadline <= now + timedelta(days=18):
            required += rem

    difference = available - required

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

    master_tasks_by_id = {
        task["page_id"]: task
        for task in tasks
    }

    completed = calculate_completed_work(
        allocations,
        master_tasks_by_id,
    )

    # Completed PFS work is preserved as history and counts toward
    # this week's 30-hour requirement.
    completed_pfs_minutes = 0
    for allocation in allocations:
        if not allocation["completed"]:
            continue

        for master_id in allocation["master_ids"]:
            task = master_tasks_by_id.get(master_id)
            if task and task["project"] == PFS_PROJECT_NAME:
                completed_pfs_minutes += workload_to_minutes(
                    allocation["allocation"],
                    task["unit"] if task["unit"] in MINUTES_PER_UNIT else "Hours",
                )
                break

    # Remove all incomplete allocations. Completed allocations are
    # never modified.
    delete_incomplete_allocations(allocations)

    # PFS weekly-hours activates only when the exact task exists
    # on Master To-Do List.
    pfs_task = next(
        (
            task for task in tasks
            if task["task"] == PFS_TASK_NAME
            and not task["completed"]
        ),
        None,
    )

    schedule_tasks._pfs_completed_minutes = completed_pfs_minutes

    new_allocations, remaining = schedule_tasks(
        tasks,
        focus_blocks,
        completed,
        pfs_task,
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

    tasks = read_master_tasks()
    all_focus_blocks, focus_blocks = read_focus_time()
    allocations = read_allocations()

    print(f"Master tasks found: {len(tasks)}")
    print(f"Focus Time blocks found: {len(focus_blocks)}")
    print(f"Task Allocations found: {len(allocations)}")

    state = load_state()

    focus_fingerprint = build_focus_fingerprint(all_focus_blocks)
    master_fingerprint = build_master_fingerprint(tasks)
    completed_allocation_fingerprint = (
        build_completed_allocation_fingerprint(allocations)
    )

    current_inputs = {
        "focus": focus_fingerprint,
        "master": master_fingerprint,
        "completed_allocations": completed_allocation_fingerprint,
    }

    force_rebuild = os.environ.get("FORCE_REBUILD", "").lower() == "true"

    changed = (
        current_inputs != {
            "focus": state.get("focus"),
            "master": state.get("master"),
            "completed_allocations": state.get(
                "completed_allocations"
            ),
        }
    )

    if not force_rebuild and not changed:
        print("No relevant changes detected.")
        print("Scheduler finished without changing the Daily Plan.")
        return

    if force_rebuild:
        print("Forced rebuild requested.")
    else:
        print("Relevant scheduling changes detected.")

    # Never rebuild while inside an active Focus Time block.
    # Crucially, we do NOT update the saved input state here.
    # That means the next run will try again after the block ends.
    if should_defer_rebuild(all_focus_blocks):
        return

    rebuild(
        tasks,
        all_focus_blocks,
        focus_blocks,
        allocations,
    )

    # Save only after a successful rebuild.
    save_state(current_inputs)

    print("Scheduler finished successfully.")


if __name__ == "__main__":
    main()
