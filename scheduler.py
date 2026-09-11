import os
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


# ============================================================
# SETTINGS
# ============================================================

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_VERSION = "2026-03-11"

TZ = ZoneInfo("America/Los_Angeles")

# Planning horizon for actual allocations
HORIZON_DAYS = 14

# Capacity/status lookahead
STATUS_LOOKAHEAD_DAYS = 18

# Normal work chunk size
MIN_CHUNK = 15
MAX_CHUNK = 45


# ============================================================
# NOTION API
# ============================================================

BASE = "https://api.notion.com/v1"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def notion(method, path, payload=None, params=None):
    response = requests.request(
        method,
        BASE + path,
        headers=HEADERS,
        json=payload,
        params=params,
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            f"Notion API {response.status_code}: {response.text}"
        )

    return response.json() if response.text else {}


# ============================================================
# NOTION SEARCH / DATABASE DISCOVERY
# ============================================================

def search_all(query=""):
    results = []
    cursor = None

    while True:
        body = {"page_size": 100}

        if query:
            body["query"] = query

        if cursor:
            body["start_cursor"] = cursor

        data = notion("POST", "/search", body)

        results.extend(data.get("results", []))

        if not data.get("has_more"):
            return results

        cursor = data.get("next_cursor")


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
    database = notion(
        "GET",
        f"/databases/{database_id}"
    )

    sources = database.get("data_sources", [])

    if not sources:
        raise RuntimeError(
            f"Database {database_id} has no data source."
        )

    return sources[0]["id"]


def get_schema(data_source_id):
    data = notion(
        "GET",
        f"/data_sources/{data_source_id}"
    )

    return data.get("properties", {})


# ============================================================
# QUERY ALL PAGES
# ============================================================

def all_pages(data_source_id):
    results = []
    cursor = None

    while True:
        body = {
            "page_size": 100
        }

        if cursor:
            body["start_cursor"] = cursor

        data = notion(
            "POST",
            f"/data_sources/{data_source_id}/query",
            body
        )

        results.extend(
            [
                page
                for page in data.get("results", [])
                if page.get("object") == "page"
            ]
        )

        if not data.get("has_more"):
            return results

        cursor = data.get("next_cursor")


# ============================================================
# PROPERTY HELPERS
# ============================================================

def prop(page, name):
    return page.get("properties", {}).get(name, {})


def title_value(page, name):
    p = prop(page, name)

    if p.get("type") == "title":
        return "".join(
            item.get("plain_text", "")
            for item in p.get("title", [])
        )

    return ""


def text_value(page, name):
    p = prop(page, name)

    if p.get("type") == "rich_text":
        return "".join(
            item.get("plain_text", "")
            for item in p.get("rich_text", [])
        )

    if p.get("type") == "select":
        return (p.get("select") or {}).get("name", "")

    if p.get("type") == "status":
        return (p.get("status") or {}).get("name", "")

    return ""


def number_value(page, name):
    p = prop(page, name)

    if p.get("type") == "number":
        return p.get("number")

    return None


def checkbox_value(page, name):
    p = prop(page, name)

    if p.get("type") == "checkbox":
        return bool(p.get("checkbox"))

    return False


def relation_ids(page, name):
    p = prop(page, name)

    if p.get("type") == "relation":
        return [
            item["id"]
            for item in p.get("relation", [])
        ]

    return []


def date_value(page, name):
    p = prop(page, name)

    if p.get("type") != "date":
        return None

    return p.get("date")


# ============================================================
# DATE HELPERS
# ============================================================

def parse_datetime(value):
    if not value:
        return None

    text = value

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ)


def task_deadline(page):
    data = date_value(page, "Deadline")

    if not data:
        return None

    return parse_datetime(data.get("start"))


def focus_interval(page):
    data = date_value(page, "Date")

    if not data:
        return None

    start = parse_datetime(data.get("start"))

    if not start:
        return None

    end = parse_datetime(data.get("end"))

    if not end:
        end = start + timedelta(minutes=60)

    return start, end


# ============================================================
# WORKLOAD CONVERSIONS
# ============================================================

def build_conversion_map(pages):
    conversions = {}

    for page in pages:
        unit = text_value(page, "Unit")
        minutes = number_value(page, "Minutes per Unit")

        if unit and minutes is not None:
            conversions[unit] = float(minutes)

    return conversions


def calculate_task_minutes(page, conversions):
    workload = number_value(page, "Workload")
    unit = text_value(page, "Unit")

    if workload is None or not unit:
        return None

    if unit not in conversions:
        return None

    return float(workload) * conversions[unit]


# ============================================================
# DISPLAY
# ============================================================

def allocation_name(task_name, minutes, unit, conversions):
    minutes_per_unit = conversions.get(unit)

    # Natural-unit tasks
    if (
        minutes_per_unit
        and minutes_per_unit > 0
        and unit not in ("Hours", "Minutes")
    ):
        units = int(round(minutes / minutes_per_unit))

        if units > 0:
            return (
                f"{task_name} — "
                f"{units} {unit.lower()}"
            )

    # Time-based tasks
    hours = minutes // 60
    remaining_minutes = minutes % 60

    if hours and remaining_minutes:
        amount = f"{hours}h {remaining_minutes}m"
    elif hours:
        amount = f"{hours}h"
    else:
        amount = f"{remaining_minutes} min"

    return f"{task_name} — {amount}"


# ============================================================
# SCHEDULING LOGIC
# ============================================================

def priority_multiplier(priority):
    return {
        "High": 1.25,
        "Medium": 1.05,
        "Low": 1.0,
    }.get(priority, 1.0)


def deadline_urgency(deadline, now):
    if not deadline:
        return 0.05

    hours_remaining = (
        deadline - now
    ).total_seconds() / 3600

    if hours_remaining <= 0:
        return 1000.0

    return 100.0 / ((hours_remaining + 1.0) ** 2)


def future_capacity_before(blocks, deadline, now):
    total = 0

    for block in blocks:

        if block["start"] >= deadline:
            continue

        end = min(
            block["end"],
            deadline
        )

        start = max(
            block["start"],
            now
        )

        if end > start:
            total += int(
                (end - start).total_seconds() / 60
            )

    return total


def qualifies(task, blocks, now):
    """
    A task qualifies when it has meaningful scheduling risk.

    The 14-day horizon is NOT itself the qualification rule.
    A task can qualify earlier when its size makes future
    capacity vulnerable.
    """

    if task["remaining"] <= 0:
        return False

    deadline = task["deadline"]

    # Undated tasks do not consume scarce scheduling capacity
    # merely because empty Focus Time exists.
    if deadline is None:
        return False

    # Overdue work always qualifies.
    if deadline <= now:
        return True

    future_capacity = future_capacity_before(
        blocks,
        deadline,
        now
    )

    # Larger tasks receive a larger resilience margin.
    resilience_margin = max(
        60,
        task["remaining"] * 0.25
    )

    # Start early enough that losing some future Focus Time
    # does not immediately put the task at risk.
    return (
        task["remaining"]
        >
        max(
            0,
            future_capacity - resilience_margin
        )
    )


def task_score(task, blocks, now):
    urgency = deadline_urgency(
        task["deadline"],
        now
    )

    size_risk = min(
        2.0,
        task["remaining"] / 180.0
    )

    return (
        urgency
        * priority_multiplier(task["priority"])
        + size_risk
    )


# ============================================================
# TASK ALLOCATION DATABASE
# ============================================================

def allocation_properties(
    schema,
    name,
    task_id,
    focus_id,
    minutes
):
    properties = {}

    for property_name, definition in schema.items():

        property_type = definition.get("type")

        if (
            property_name == "Name"
            and property_type == "title"
        ):
            properties[property_name] = {
                "title": [
                    {
                        "type": "text",
                        "text": {
                            "content": name
                        }
                    }
                ]
            }

        elif (
            property_name == "Master To-do list"
            and property_type == "relation"
        ):
            properties[property_name] = {
                "relation": [
                    {
                        "id": task_id
                    }
                ]
            }

        elif (
            property_name == "Focus time"
            and property_type == "relation"
        ):
            properties[property_name] = {
                "relation": [
                    {
                        "id": focus_id
                    }
                ]
            }

        elif (
            property_name == "Allocation"
            and property_type == "number"
        ):
            properties[property_name] = {
                "number": minutes
            }

        elif (
            property_name == "Completion"
            and property_type == "checkbox"
        ):
            properties[property_name] = {
                "checkbox": False
            }

    return properties


def create_allocation(
    data_source_id,
    properties
):
    return notion(
        "POST",
        "/pages",
        {
            "parent": {
                "type": "data_source_id",
                "data_source_id": data_source_id
            },
            "properties": properties
        }
    )


def archive_page(page_id):
    notion(
        "PATCH",
        f"/pages/{page_id}",
        {
            "in_trash": True
        }
    )


# ============================================================
# MAIN SCHEDULER
# ============================================================

def main():

    now = datetime.now(TZ)

    planning_end = (
        now
        + timedelta(days=HORIZON_DAYS)
    )

    status_end = (
        now
        + timedelta(days=STATUS_LOOKAHEAD_DAYS)
    )

    # --------------------------------------------------------
    # Find the four databases
    # --------------------------------------------------------

    master_database = find_database(
        "Master To-Do List"
    )

    focus_database = find_database(
        "Focus time"
    )

    allocation_database = find_database(
        "Task Allocations"
    )

    conversion_database = find_database(
        "Workload Conversions"
    )

    # --------------------------------------------------------
    # Get their data sources
    # --------------------------------------------------------

    master_ds = get_data_source(
        master_database
    )

    focus_ds = get_data_source(
        focus_database
    )

    allocation_ds = get_data_source(
        allocation_database
    )

    conversion_ds = get_data_source(
        conversion_database
    )

    # --------------------------------------------------------
    # Read everything
    # --------------------------------------------------------

    master_pages = all_pages(
        master_ds
    )

    focus_pages = all_pages(
        focus_ds
    )

    allocation_pages = all_pages(
        allocation_ds
    )

    conversion_pages = all_pages(
        conversion_ds
    )

    conversion_map = build_conversion_map(
        conversion_pages
    )

    allocation_schema = get_schema(
        allocation_ds
    )

    # --------------------------------------------------------
    # Preserve completed allocation work
    # --------------------------------------------------------

    completed_minutes = {}

    for allocation in allocation_pages:

        if not checkbox_value(
            allocation,
            "Completion"
        ):
            continue

        task_ids = relation_ids(
            allocation,
            "Master To-do list"
        )

        minutes = (
            number_value(
                allocation,
                "Allocation"
            )
            or 0
        )

        for task_id in task_ids:

            completed_minutes[task_id] = (
                completed_minutes.get(
                    task_id,
                    0
                )
                + float(minutes)
            )

    # --------------------------------------------------------
    # Remove old unfinished allocations
    #
    # Completed allocations stay permanently.
    # Everything unfinished is regenerated from current data.
    # --------------------------------------------------------

    for allocation in allocation_pages:

        if checkbox_value(
            allocation,
            "Completion"
        ):
            continue

        archive_page(
            allocation["id"]
        )

    # --------------------------------------------------------
    # Build active task list
    # --------------------------------------------------------

    tasks = []

    for page in master_pages:

        if checkbox_value(
            page,
            "Completed"
        ):
            continue

        total_minutes = calculate_task_minutes(
            page,
            conversion_map
        )

        if (
            total_minutes is None
            or total_minutes <= 0
        ):
            continue

        remaining = max(
            0.0,
            total_minutes
            - completed_minutes.get(
                page["id"],
                0
            )
        )

        if remaining <= 0:
            continue

        tasks.append(
            {
                "id": page["id"],
                "name": title_value(
                    page,
                    "Task"
                ),
                "deadline": task_deadline(
                    page
                ),
                "priority": text_value(
                    page,
                    "Priority Level"
                ),
                "unit": text_value(
                    page,
                    "Unit"
                ),
                "continuous": checkbox_value(
                    page,
                    "Continuous"
                ),
                "remaining": remaining,
            }
        )

    # --------------------------------------------------------
    # Build available Focus Time blocks
    # --------------------------------------------------------

    blocks = []

    for page in focus_pages:

        interval = focus_interval(
            page
        )

        if not interval:
            continue

        start, end = interval

        if end <= now:
            continue

        if start > planning_end:
            continue

        start = max(
            start,
            now
        )

        end = min(
            end,
            planning_end
        )

        if end <= start:
            continue

        blocks.append(
            {
                "id": page["id"],
                "start": start,
                "end": end,
            }
        )

    blocks.sort(
        key=lambda block: block["start"]
    )

    # --------------------------------------------------------
    # Allocate work block by block
    # --------------------------------------------------------

    created_allocations = 0

    for block in blocks:

        available = int(
            (
                block["end"]
                - block["start"]
            ).total_seconds() / 60
        )

        if available < MIN_CHUNK:
            continue

        # Work that independently qualifies right now.
        candidates = [
            task
            for task in tasks
            if qualifies(
                task,
                blocks,
                now
            )
            and task["remaining"] > 0
        ]

        candidates.sort(
            key=lambda task:
                task_score(
                    task,
                    blocks,
                    now
                ),
            reverse=True
        )

        # Prevent one task from monopolizing a block
        # when multiple tasks qualify.
        touched_this_block = set()

        while (
            available >= MIN_CHUNK
            and candidates
        ):

            unused = [
                task
                for task in candidates
                if (
                    task["id"]
                    not in touched_this_block
                    and task["remaining"] > 0
                )
            ]

            if not unused:

                touched_this_block.clear()

                unused = [
                    task
                    for task in candidates
                    if task["remaining"] > 0
                ]

            if not unused:
                break

            task = max(
                unused,
                key=lambda item:
                    task_score(
                        item,
                        blocks,
                        now
                    )
            )

            # ------------------------------------------------
            # Continuous tasks cannot be split.
            # ------------------------------------------------

            if task["continuous"]:

                amount = int(
                    math.ceil(
                        task["remaining"]
                    )
                )

                if amount > available:
                    touched_this_block.add(
                        task["id"]
                    )
                    continue

            # ------------------------------------------------
            # Normal tasks are allocated in natural units
            # whenever possible.
            # ------------------------------------------------

            else:

                target = min(
                    MAX_CHUNK,
                    available,
                    task["remaining"]
                )

                minutes_per_unit = (
                    conversion_map.get(
                        task["unit"],
                        1
                    )
                )

                if (
                    minutes_per_unit > 0
                    and task["unit"]
                    not in (
                        "Hours",
                        "Minutes"
                    )
                ):

                    units = int(
                        target
                        // minutes_per_unit
                    )

                    amount = int(
                        units
                        * minutes_per_unit
                    )

                    # If one natural unit is itself
                    # larger than 15 minutes, allow it.
                    if (
                        amount < MIN_CHUNK
                        and minutes_per_unit <= available
                    ):
                        amount = int(
                            minutes_per_unit
                        )

                else:
                    amount = int(target)

            if (
                amount <= 0
                or amount > available
            ):
                touched_this_block.add(
                    task["id"]
                )
                continue

            # ------------------------------------------------
            # Create allocation
            # ------------------------------------------------

            display_name = allocation_name(
                task["name"],
                amount,
                task["unit"],
                conversion_map
            )

            properties = allocation_properties(
                allocation_schema,
                display_name,
                task["id"],
                block["id"],
                amount
            )

            create_allocation(
                allocation_ds,
                properties
            )

            created_allocations += 1

            task["remaining"] -= amount

            available -= amount

            touched_this_block.add(
                task["id"]
            )

    # ========================================================
    # CAPACITY / GO-STOP
    # ========================================================

    required_minutes = 0

    for task in tasks:

        if (
            task["deadline"]
            and task["deadline"] <= status_end
        ):
            required_minutes += (
                task["remaining"]
            )

    available_minutes = 0

    for block in blocks:

        available_minutes += int(
            (
                block["end"]
                - block["start"]
            ).total_seconds() / 60
        )

    shortfall = max(
        0,
        math.ceil(
            required_minutes
            - available_minutes
        )
    )

    # ========================================================
    # GITHUB ACTIONS OUTPUT
    # ========================================================

    if shortfall:

        hours, minutes = divmod(
            shortfall,
            60
        )

        print(
            f"STOP — {hours}h "
            f"{minutes}m additional "
            f"Focus Time needed."
        )

    else:

        print(
            "GO — enough Focus Time "
            "for the current workload."
        )

    print(
        f"Active tasks: {len(tasks)}"
    )

    print(
        f"Focus Time blocks: {len(blocks)}"
    )

    print(
        f"New allocations: "
        f"{created_allocations}"
    )


if __name__ == "__main__":
    main()
