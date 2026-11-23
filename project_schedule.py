"""Scheduled runs of an AI Project Center project: the recurrence, its next time, its words, what one
occurrence does, and the program a systemd user timer starts.

A schedule is a small dict stored in the project record (project_center.py, key `schedule`):

    {"kind": "daily",    "time": "05:00"}
    {"kind": "weekdays", "time": "05:00", "days": ["mon", "wed", "fri"]}
    {"kind": "weekly",   "time": "05:00", "day": "tue"}
    {"kind": "hours",    "time": "01:30", "every": 6}       # 01:30, 07:30, 13:30, 19:30
    {"kind": "monthly",  "time": "05:00", "day": 31}        # or "last"; months without a 31st: none
    ... each optionally with "until": "YYYY-MM-DD" (the last day it may run, local time)

`check()` is the only way in: exact keys, exact types, canonical order, so a stored schedule always
turns into the same systemd calendar (`on_calendar`) and the same next time (`next_run`). Times are
local wall-clock times, as systemd's OnCalendar reads them: a time that does not exist on a day (the
hour skipped when summer time begins) does not run that day, an hour that happens twice runs once.

One occurrence (`trigger`) goes through the very calls a manual Run makes (ProjectCenter.call "get",
then "run" with `scheduled`), so the approval, Settings › Rules and the supervisor apply unchanged.
Whatever stops it is recorded on the project ("schedule_note") and said in a desktop notification.

As a program (`python3 -I project_schedule.py run <project id>`, the timer's service) it hands the
occurrence to whoever holds the project store, or holds it itself (schedule_handover.py);
`clear` removes every timer of the plugin (schedule_units.remove_all)."""

from __future__ import annotations

import datetime as dt
import os
import re
import sys

KINDS = ("daily", "weekdays", "weekly", "hours", "monthly")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Divisors of 24 only: the runs keep the same hours every day (systemd's calendar restarts at midnight).
EVERY_HOURS = (1, 2, 3, 4, 6, 8, 12)
MAX_SCHEDULE_BYTES = 512
SEARCH_DAYS = 400              # far enough for a monthly 31st or a leap day; a schedule never needs more
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_KEYS = {"daily": {"time"}, "weekdays": {"time", "days"}, "weekly": {"time", "day"},
         "hours": {"time", "every"}, "monthly": {"time", "day"}}
_ACTIVE = {"planning", "running", "reviewing", "pausing"}
_STOPPED = {"paused", "interrupted", "failed"}


# --- the schedule itself -----------------------------------------------------------------------------

def check(value) -> dict | None:
    """The schedule in its canonical form, None for off; ValueError for anything else (unknown keys,
    bools as numbers, a weekday twice, an impossible date)."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("kind") not in KINDS:
        raise ValueError("schedule")
    kind = value["kind"]
    allowed = _KEYS[kind] | {"kind", "until"}
    if any(key not in allowed for key in value) or any(key not in value for key in _KEYS[kind]):
        raise ValueError("schedule keys")
    result = {"kind": kind, "time": _time_text(value["time"])}
    if kind == "weekdays":
        days = value["days"]
        if not isinstance(days, list) or not days or len(days) > 7 or len(set(map(str, days))) != len(days) \
                or any(day not in DAYS for day in days):
            raise ValueError("schedule days")
        result["days"] = [day for day in DAYS if day in days]
    elif kind == "weekly":
        if value["day"] not in DAYS:
            raise ValueError("schedule day")
        result["day"] = value["day"]
    elif kind == "hours":
        if type(value["every"]) is not int or value["every"] not in EVERY_HOURS:
            raise ValueError("schedule every")
        result["every"] = value["every"]
    elif kind == "monthly":
        day = value["day"]
        if day != "last" and (type(day) is not int or not 1 <= day <= 31):
            raise ValueError("schedule day")
        result["day"] = day
    until = value.get("until")
    if until is not None:
        result["until"] = until_date(until).isoformat()
    return result


def _time_text(value) -> str:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError("schedule time")
    return value


def until_date(value) -> dt.date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("schedule until")
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError("schedule until") from None
    if not 2000 <= day.year <= 9999:
        raise ValueError("schedule until")
    return day


def _clock(schedule: dict) -> tuple[int, int]:
    hour, minute = schedule["time"].split(":")
    return int(hour), int(minute)


def _hours(schedule: dict) -> list[int]:
    """The hours of a day it runs at: every N hours from the time's hour, counted from midnight."""
    hour, _minute = _clock(schedule)
    if schedule["kind"] != "hours":
        return [hour]
    every = schedule["every"]
    return list(range(hour % every, 24, every))


def runs_on(schedule: dict, day: dt.date) -> bool:
    kind = schedule["kind"]
    if kind in ("daily", "hours"):
        return True
    if kind == "weekdays":
        return DAYS[day.weekday()] in schedule["days"]
    if kind == "weekly":
        return DAYS[day.weekday()] == schedule["day"]
    if schedule["day"] == "last":
        return (day + dt.timedelta(days=1)).month != day.month
    return day.day == schedule["day"]


def ended(schedule: dict | None, today: dt.date) -> bool:
    """Its end date is behind `today` (local)."""
    return bool(schedule) and schedule.get("until") is not None and today > until_date(schedule["until"])


def _local(naive: dt.datetime, zone) -> dt.datetime | None:
    """The wall-clock time `naive` in `zone`, or None when that time does not exist there (the hour
    skipped when summer time begins: systemd's calendar does not run then either). An hour that
    happens twice is its first occurrence (fold 0)."""
    if zone is None:
        aware = naive.astimezone()                 # this computer's zone
        back = aware.astimezone().replace(tzinfo=None)
        return aware if back == naive else None
    aware = naive.replace(tzinfo=zone, fold=0)
    back = aware.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None)
    return aware if back == naive else None


def next_run(schedule: dict | None, after: dt.datetime, zone=None) -> dt.datetime | None:
    """The first time after `after` (aware) the schedule runs, aware in `zone` (None: this computer's
    zone); None when it is off or has ended."""
    if not schedule:
        return None
    until = until_date(schedule["until"]) if schedule.get("until") else None
    here = after.astimezone(zone) if zone is not None else after.astimezone()
    _hour, minute = _clock(schedule)
    for offset in range(SEARCH_DAYS):
        day = here.date() + dt.timedelta(days=offset)
        if until is not None and day > until:
            return None
        if not runs_on(schedule, day):
            continue
        for hour in _hours(schedule):
            moment = _local(dt.datetime(day.year, day.month, day.day, hour, minute), zone)
            if moment is not None and moment > after:
                return moment
    return None


def on_calendar(schedule: dict) -> str:
    """systemd's OnCalendar= for a checked schedule: built from its numbers and day names only."""
    schedule = check(schedule)
    if schedule is None:
        raise ValueError("schedule")
    _hour, minute = _clock(schedule)
    hours = ",".join(f"{hour:02d}" for hour in _hours(schedule))
    kind = schedule["kind"]
    prefix, date = "", "*-*-*"
    if kind == "weekdays":
        prefix = ",".join(day.capitalize() for day in schedule["days"]) + " "
    elif kind == "weekly":
        prefix = schedule["day"].capitalize() + " "
    elif kind == "monthly":
        date = "*-*~01" if schedule["day"] == "last" else f"*-*-{schedule['day']:02d}"
    return f"{prefix}{date} {hours}:{minute:02d}:00"


# --- words -------------------------------------------------------------------------------------------

def _ordinal(number: int) -> str:
    suffix = "th" if 11 <= number % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def recurrence_text(schedule: dict) -> str:
    """"Every day at 05:00", "Mon, Wed, Fri at 05:00", "Every 6 hours from 01:30"…"""
    kind, time = schedule["kind"], schedule["time"]
    if kind == "daily" or (kind == "weekdays" and len(schedule["days"]) == 7):
        return f"Every day at {time}"
    if kind == "weekdays":
        days = schedule["days"]
        if days == list(DAYS[:5]):
            return f"Mon–Fri at {time}"
        if days == list(DAYS[5:]):
            return f"Sat and Sun at {time}"
        return ", ".join(day.capitalize() for day in days) + f" at {time}"
    if kind == "weekly":
        return f"Every {DAY_NAMES[DAYS.index(schedule['day'])]} at {time}"
    if kind == "hours":
        first = f"{_hours(schedule)[0]:02d}:{time[3:]}"
        return f"Every hour at :{time[3:]}" if schedule["every"] == 1 else f"Every {schedule['every']} hours from {first}"
    if schedule["day"] == "last":
        return f"Monthly on the last day at {time}"
    note = " (months without it are skipped)" if schedule["day"] > 28 else ""
    return f"Monthly on the {_ordinal(schedule['day'])} at {time}{note}"


def moment_text(moment: dt.datetime, now: dt.datetime) -> str:
    """"Tue 05:00" within the coming week, else "Tue 1 Dec 05:00" (plus the year when it differs)."""
    days = (moment.date() - now.date()).days
    if 0 <= days < 7:
        return moment.strftime("%a %H:%M")
    text = f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')}"
    if moment.year != now.year:
        text += f" {moment.year}"
    return f"{text} {moment.strftime('%H:%M')}"


def summary(schedule: dict | None, next_at: str | None, now: dt.datetime | None = None) -> str:
    """The project page's line: "Every day at 05:00 · next run Tue 05:00" (or "· ended")."""
    if not schedule:
        return "Not scheduled"
    now = now or dt.datetime.now().astimezone()
    parts = [recurrence_text(schedule)]
    if schedule.get("until"):
        day = until_date(schedule["until"])
        parts.append(f"until {day.day} {day.strftime('%b')} {day.year}")
    moment = parse_moment(next_at)
    parts.append(f"next run {moment_text(moment.astimezone(now.tzinfo), now)}" if moment else "ended")
    return " · ".join(parts)


def parse_moment(value) -> dt.datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


# --- one occurrence ----------------------------------------------------------------------------------

# Why an occurrence did not run: (event text, notify). "running" is only recorded: a long run of an
# hourly schedule would otherwise notify every hour.
REASONS = {
    "not_approved": ("Scheduled run skipped: the plan is not approved, or the plan or team changed since it was "
                     "approved. Approve the plan again to let the schedule run it.", True),
    "running": ("Scheduled run skipped: the project was still running.", False),
    "stopped": ("Scheduled run skipped: the last run stopped before it ended. Continue it or start it again "
                "yourself; the schedule runs it again after that.", True),
    "review_changes": ("Scheduled run skipped: review the existing changes of the interrupted run first.", True),
    "ended": ("The schedule has ended: its last day has passed.", False),
    "failed": ("Scheduled run could not start: {detail}", True),
}


def trigger(call, project_id: str, notify, today: dt.date | None = None) -> str:
    """One occurrence for `project_id` through `call` (ProjectCenter.call, its owner's): "started", or
    why not (a REASONS key, "unscheduled", "unknown"). A skip is recorded on the project and, where
    REASONS says so, notified (not again while the same reason repeats)."""
    import project_supervisor
    for attempt in range(3):
        try:
            view = call("get", {"id": project_id})["project"]
        except project_supervisor.ProjectError as error:
            return "unknown" if error.code == "project_unknown" else _failed(call, project_id, notify, error)
        reason = _blocked(view, today or dt.date.today())
        if reason == "unscheduled":
            return reason
        if reason is not None:
            return _skip(call, view, notify, reason)
        try:
            call("run", {"id": project_id, "revision": view["revision"], "scheduled": True})
            return "started"
        except project_supervisor.ProjectError as error:
            if error.code == "project_changed" and attempt < 2:
                continue                   # saved meanwhile: look again
            reason = {"project_already_running": "running", "project_still_pausing": "running",
                      "project_plan_not_approved": "not_approved",
                      "project_review_changes_first": "review_changes",
                      "project_not_scheduled": "unscheduled"}.get(error.code)
            if reason == "unscheduled":
                return reason
            if reason is None:
                return _failed(call, project_id, notify, error, view)
            return _skip(call, view, notify, reason)
    return _failed(call, project_id, notify, project_supervisor.ProjectError("project_changed"))


def _blocked(view: dict, today: dt.date) -> str | None:
    """Why this project must not run now, from its view; None: ask "run" (which checks it all again)."""
    schedule = view.get("schedule")
    if not schedule:
        return "unscheduled"
    if ended(schedule, today):
        return "ended"
    status = view.get("status")
    if status in _ACTIVE or view.get("processes_stopping") is True:
        return "running"
    if view.get("needs_reconciliation") is True:
        return "review_changes"
    if status in _STOPPED:
        return "stopped"
    # The approval's digest (plan, team, folder, permissions) is checked by "run" itself on a fresh look
    # at the agents; the display's approval_valid may be older or unknown.
    if status not in ("ready", "completed") or view.get("approved_version") is None \
            or view.get("approved_version") != view.get("plan_version"):
        return "not_approved"
    return None


def _skip(call, view: dict, notify, reason: str, detail: str = "") -> str:
    text, loud = REASONS[reason]
    text = text.format(detail=detail)
    repeated = False
    try:
        answer = call("schedule_note", {"id": view["id"], "reason": reason, "text": text})
        repeated = answer.get("repeated") is True
    except Exception:
        pass                              # not recorded (the store refused it): the notification still says it
    if loud and not repeated:
        notify(f"{view.get('title') or 'Project'}: scheduled run skipped" if reason != "failed"
               else f"{view.get('title') or 'Project'}: scheduled run failed", text)
    return reason


def _failed(call, project_id: str, notify, error, view: dict | None = None) -> str:
    import project_texts
    detail = project_texts.failure(error)
    if view is None:
        notify("Scheduled run failed", f"A scheduled project could not start: {detail}")
        return "failed"
    return _skip(call, view, notify, "failed", detail)


def notify(title: str, body: str, run=None) -> bool:
    """A desktop notification as plain text (notify-send, argv, 5 s at most; `--` before the texts,
    so a title starting with "-" is no option)."""
    import ai_core
    run = run or ai_core.read_bounded_output
    _out, ok = run(["notify-send", "--app-name=AI Agents Map", "--icon=alarm-symbolic", "--",
                    ai_core.plain_text(title, 80), ai_core.plain_text(body, 300)], max_bytes=4096, timeout=5.0)
    return ok


# --- the timer's program -----------------------------------------------------------------------------

def clear(open_store=None, remove_all=None) -> int:
    """Every schedule off and every timer of the plugin gone (before removing the plugin). Best effort
    over everything: each step is tried whatever failed before it, every failure is said, and the exit
    status is 1 if any step failed or a timer of ours is still there (remove_all reads the folder
    again). The timers are removed while clear still holds the store, and only then is it let go.
    While another program holds the store (the app, or a timer's run), nothing is touched and the exit
    status is 1: that owner would put its timers back."""
    import project_center
    import schedule_units
    from project_supervisor import ProjectError
    remove_all = remove_all or schedule_units.remove_all
    problems: list[str] = []
    failures: dict = {}

    def remove() -> None:
        try:
            failures.update(remove_all() or {})
        except Exception as error:
            failures["all"] = _text_of(error)
    try:
        center = (open_store or project_center.ProjectCenter)()
    except ProjectError as error:
        if error.code == "project_center_busy":
            print("The project list is in use (AI Agents Map or a scheduled run is running), and it would put "
                  "the timers back: nothing was changed. Close AI Agents Map, wait for scheduled runs to end, "
                  "and run clear again.", file=sys.stderr)
            return 1
        problems.append(f"The project list could not be opened: {_text_of(error)}")
        remove()
    except Exception as error:
        problems.append(f"The project list could not be opened: {_text_of(error)}")
        remove()
    else:
        # The timers go while clear still holds the store: no other program can open it and set a
        # schedule in between, so nothing it wants is removed behind its back.
        try:
            try:
                for project_id in center.schedules():
                    try:
                        center.call("set_schedule", {"id": project_id, "schedule": None})
                    except Exception as error:
                        problems.append(f"The schedule of project {project_id} could not be turned off: "
                                        f"{_text_of(error)}")
            except Exception as error:
                problems.append(f"The schedules could not be read: {_text_of(error)}")
            remove()
        finally:
            try:
                center.shutdown()
            except Exception as error:
                problems.append(f"The project list could not be closed: {_text_of(error)}")
    for project_id, why in sorted(failures.items()):
        print(f"The timer of project {project_id} could not be removed: {why}", file=sys.stderr)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if failures or problems else 0


def _text_of(error) -> str:
    code = getattr(error, "code", None)
    return str(code or error or type(error).__name__)[:300]


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "run" and re.fullmatch(r"[0-9a-f]{32}", argv[1]):
        import schedule_handover
        return schedule_handover.job(argv[1])
    if argv == ["clear"]:
        return clear()
    print("usage: project_schedule.py run <project id> | clear", file=sys.stderr)
    return 2


if __name__ == "__main__":
    # Run with -I (no script folder on sys.path): the plugin's own modules come from its own folder.
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    sys.exit(main(sys.argv[1:]))
