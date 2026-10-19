"""Project real application activity without deleting local test history."""


def application_events(events, demo_job_ids):
    hidden = set()
    demo_run = False
    run_types = {"run_started", "application_progress", "run_finished", "run_interrupted", "stop_requested"}
    # Legacy demo runs used ordinary run event types. Their owned start marker
    # identifies the corresponding run events, while unrelated saves remain.
    for index, event in sorted(enumerate(events), key=lambda item: item[1]["id"]):
        kind = str(event.get("type", ""))
        if kind in {"run_started", "demo_run_started"}:
            demo_run = str(event.get("message", "")).startswith("Local demo worker started")
        if kind == "demo" or kind.startswith("demo_") or event.get("job_id") in demo_job_ids or (demo_run and kind in run_types):
            hidden.add(index)
        if kind in {"run_finished", "run_interrupted", "demo_run_finished", "demo_run_interrupted"}:
            demo_run = False
    return [event for index, event in enumerate(events) if index not in hidden]
