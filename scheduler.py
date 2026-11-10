
yes, i will create a filter which does not include them, and then a view which is only for completed tasks.
Perfect. That's exactly how I'd handle it.

We'll have Python exclude completed tasks from ranking, but leave them untouched in the database. Your two Notion views will handle visibility:

Daily Plan: Completed unchecked, sorted by Schedule Order ascending.

Completed Tasks: Completed checked, sorted by completion date if you eventually add one, or simply by due date.

Our agreed setup
Property

Type

Python's role

Task

Title

Reads

Category

Select

Reads

Due Date

Date

Reads

Estimated Time

Select

Converts to minutes

Notes

Text

Leaves alone

Submitted

Checkbox

Reads

Completed

Checkbox

Excludes checked tasks

Schedule Order

Number

Writes

One addition: we'll need a Completed checkbox in Task Submission if you haven't created it already.

Next step
Before I rewrite scheduler.py, let's confirm the database schema is in place. Have you created Schedule Order and Completed in Task Submission yet?




i created a completed checkbox. do i need to create a new property for it to input into?
Yes! You need one more property in Task Submission:

Property

Type

Schedule Order

Number

Python will write the ranking into this property (1, 2, 3, etc.).

You already have Completed as a checkbox, so you don't need to create anything else for that.

Once you've created Schedule Order, we'll be ready to adapt the Python code.




User attachment
here you go
Excellent! Your Task Submission database is almost ready. I can see that you've created both properties we needed:

Ready Completed — checkbox

Ready Schedule Order — number

I can also see Task, Due Date, Category, Estimated Time, and Notes. Those are all in place.

One property is still missing: Submitted (Checkbox).

I'd keep it because it lets you distinguish between tasks you're still entering and tasks ready for Python to rank.

Add that one property, and we'll have the complete schema.

I also see a Formula property. We can leave it alone for now; I just want to know what it does before we decide whether to remove it.

i don't think that's necessary, do you>
You're right. We don't need Submitted if we want to keep things simple.

Python can rank all incomplete tasks in Task Submission automatically, without a separate submission flag.

That means:

New tasks are included automatically.

Completed tasks are excluded.

No extra checkbox to manage.

One caveat: unfinished task entries will also be ranked. But if you're comfortable with that, I'd leave Submitted out.

Your existing properties are enough. Let's keep the database as it is.




great. can you give me the script?

Worked for 30s
I've rebuilt your scheduler as a much leaner script.

scheduler.py

New standalone Python scheduler

Download scheduler.py

Created
What this version does
Reads tasks directly from Task Submission.

Excludes tasks marked Completed.

Ranks overdue and approaching deadlines first.

Considers estimated time when ranking tasks.

Writes numerical rankings into Schedule Order.

Avoids unnecessary Notion updates when the ranking hasn't changed.

Handles your time estimates, including custom minute increments and 3+ hours.

What it removes
No more:

Separate category databases.

Task Allocations.

Priority or Bump.

Workload and Unit calculations.

Dependency tracking.

Task creation, deletion, or archiving.

Before we install it
Don't replace your existing script yet. First, make a backup.

Then we'll replace the old scheduler.py in GitHub and update your workflow to run once each morning at 5 AM Pacific, with manual triggering available.

One important note: this version has been syntax-checked, but hasn't yet been tested against your live Notion database. We'll verify the connection and property names before relying on its rankings.


scheduler.py
Code
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



