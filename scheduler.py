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

    # Update existing rows in place; create a row only if none exists.
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
