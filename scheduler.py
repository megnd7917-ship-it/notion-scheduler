import os
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests


# ============================================================
# SETTINGS
# ============================================================

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_VERSION = "2026-03-11"  # Retains the API version used by your existing script.
DATABASE_NAME = "Task Submission"
TASK_PROPERTY = "Task"
DUE_DATE_PROPERTY = "Due Date"
ESTIMATED_TIME_PROPERTY = "Estimated Time"
COMPLETED_PROPERTY = "Completed"
SCHEDULE_ORDER_PROPERTY = "Schedule Order"
TIME_ZONE = ZoneInfo("America/Los_Angeles")

MIN_REQUEST_INTERVAL_SECONDS = 0.4
MAX_RATE_LIMIT_RETRIES = 7
_LAST_REQUEST_AT = 0.0

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# The final option is treated as a minimum estimate of 180 minutes.
# Add or edit labels here if you change the Notion select options.
ESTIMATED_MINUTES = {
    "5 minutes": 5,
    "10 minutes": 10,
    "15 minutes": 15,
    "20 minutes": 20,
    "25 minutes": 25,
    "30 minutes": 30,
    "45 minutes": 45,
    "1 hour": 60,
    "1.5 hours": 90,
    "2 hours": 120,
    "3+ hours": 180,
}


def notion(method, path, **kwargs):
    """Make a paced Notion API request with retry handling for rate limits."""
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
            return response.json() if response.content else {}

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                data = response.json()
                retry_after = retry_after or data.get("additional_data", {}).get("retry_after")
                wait_seconds = float(retry_after) if retry_after is not None else min(5 * (2 ** attempt), 60)
            except (ValueError, TypeError):
                wait_seconds = min(5 * (2 ** attempt), 60)

            if attempt >= MAX_RATE_LIMIT_RETRIES:
                raise RuntimeError(
                    f"Notion rate limit persisted after {MAX_RATE_LIMIT_RETRIES} retries: {response.text}"
                )

            wait_seconds = max(1.0, wait_seconds) + 0.25
            print(f"Rate limited by Notion. Waiting {wait_seconds:g} seconds...")
            time.sleep(wait_seconds)
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


def plain_title(items):
    return "".join(item.get("plain_text", "") for item in items or []).strip()


def find_data_source(database_name):
    """Find the database by exact title and return its first data-source ID."""
    for item in search_all(database_name):
        if item.get("object") == "database":
            if plain_title(item.get("title")) != database_name:
                continue
            database = item
        elif item.get("object") == "data_source":
            database_id = item.get("parent", {}).get("database_id")
            if not database_id:
                continue
                
             database = notion("GET", f"databases/{database_id}")
            if plain_title(database.get("title")) != database_name:
                continue
        else:
            continue

        data_sources = database.get("data_sources", [])
        if not data_sources:
            database = notion("GET", f"databases/{database['id']}")
            data_sources = database.get("data_sources", [])
        if not data_sources:
            raise RuntimeError(f'No data source found for database "{database_name}".')
        return data_sources[0]["id"]

    raise RuntimeError(
        f'Could not find database "{database_name}". Check its name and make sure the Notion integration has access to it.'
    )


def get_schema(data_source_id):
    data_source = notion("GET", f"data_sources/{data_source_id}")
    return data_source.get("properties", {})


def validate_schema(properties):
    required = {
        TASK_PROPERTY: "title",
        DUE_DATE_PROPERTY: "date",
        ESTIMATED_TIME_PROPERTY: "select",
        COMPLETED_PROPERTY: "checkbox",
        SCHEDULE_ORDER_PROPERTY: "number",
    }
    problems = []
    for name, expected_type in required.items():
        actual = properties.get(name, {}).get("type")
        if actual != expected_type:
            problems.append(f'"{name}" should be {expected_type}, found {actual or "missing"}')
    if problems:
        raise RuntimeError("Task Submission database schema needs attention:\n- " + "\n- ".join(problems))


def query_all_pages(data_source_id):
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


def get_title(page, property_name):
    prop = page.get("properties", {}).get(property_name, {})
    if prop.get("type") != "title":
        return ""
    return plain_title(prop.get("title"))


def get_checkbox(page, property_name):
    prop = page.get("properties", {}).get(property_name, {})
    return bool(prop.get("checkbox", False)) if prop.get("type") == "checkbox" else False


def get_select(page, property_name):
    prop = page.get("properties", {}).get(property_name, {})
    if prop.get("type") != "select" or not prop.get("select"):
        return None
    return prop["select"].get("name")


def get_date(page, property_name):
    prop = page.get("properties", {}).get(property_name, {})
    if prop.get("type") != "date" or not prop.get("date"):
        return None
    start = prop["date"].get("start")
    if not start:
        return None
    # Notion date-only values are YYYY-MM-DD. If a time is present, use its local date.
    return date.fromisoformat(start[:10])


def estimate_minutes(label):
    """Convert a Notion select label to minutes, including custom increments."""
    if not label:
        return 0
    if label in ESTIMATED_MINUTES:
        return ESTIMATED_MINUTES[label]

    normalized = label.strip().casefold()
    if normalized in {"3+ hours", "3 + hours", "3 or more hours"}:
        return 180

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?)", normalized)
    if match:
        return int(round(float(match.group(1))))

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?)", normalized)
    if match:
        return int(round(float(match.group(1)) * 60))

    raise RuntimeError(
        f'Cannot interpret Estimated Time option "{label}". '
        'Use a label such as "25 minutes", "1 hour", "1.5 hours", or "3+ hours".'
    )


def urgency_bucket(due_date, today):
    if due_date is None:
        return 7
    days_left = (due_date - today).days
    if days_left < 0:
        return 0  # Overdue
    if days_left == 0:
        return 1  # Due today
    if days_left == 1:
        return 2  # Due tomorrow
    if days_left <= 3:
        return 3  # Due in 2-3 days
    if days_left <= 7:
        return 4  # Due this week
    if days_left <= 14:
        return 5  # Due in the next two weeks
    return 6      # Later deadline


def task_sort_key(task, today):
    due_date = task["due_date"]
    minutes = task["minutes"]
    bucket = urgency_bucket(due_date, today)

    if due_date is None:
        workload_pressure = 0
    else:
        days_left = max(1, (due_date - today).days)
        workload_pressure = minutes / days_left

    # Deadline group first; within a group, prioritize more work per day remaining.
    return (
        bucket,
        -workload_pressure,
        -minutes,
        task["name"].casefold(),
    )


def read_active_tasks(pages):
    tasks = []
    completed_count = 0

    for page in pages:
        name = get_title(page, TASK_PROPERTY)
        if not name:
            continue

        if get_checkbox(page, COMPLETED_PROPERTY):
            completed_count += 1
            continue

        estimate_label = get_select(page, ESTIMATED_TIME_PROPERTY)
        tasks.append({
            "page_id": page["id"],
            "name": name,
            "due_date": get_date(page, DUE_DATE_PROPERTY),
            "minutes": estimate_minutes(estimate_label),
            "schedule_order": page.get("properties", {}).get(SCHEDULE_ORDER_PROPERTY, {}).get("number"),
        })

    print(f"Incomplete tasks found: {len(tasks)}")
    print(f"Completed tasks skipped: {completed_count}")
    return tasks


def write_schedule_order(tasks):
    """Write ranks 1..N, avoiding API writes when the value is already correct."""
    changed = 0
    for rank, task in enumerate(tasks, start=1):
        if task["schedule_order"] == rank:
            continue

        notion(
            "PATCH",
            f"pages/{task['page_id']}",
            json={
                "properties": {
                    SCHEDULE_ORDER_PROPERTY: {"number": rank}
                }
            },
        )
        changed += 1
        print(f'{rank:>3}. {task["name"]}')

    print(f"Schedule updated. Property changes made: {changed}")


def main():
    print("=" * 56)
    print("NOTION TASK RANKER")
    print("=" * 56)

    data_source_id = find_data_source(DATABASE_NAME)
    schema = get_schema(data_source_id)
    validate_schema(schema)

    pages = query_all_pages(data_source_id)
    print(f"Total pages read: {len(pages)}")

    tasks = read_active_tasks(pages)
    today = datetime.now(TIME_ZONE).date()
    tasks.sort(key=lambda task: task_sort_key(task, today))

    write_schedule_order(tasks)
    print("Done.")


if __name__ == "__main__":
    main()
