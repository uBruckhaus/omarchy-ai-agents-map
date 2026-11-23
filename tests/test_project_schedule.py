"""Scheduled project runs: the recurrence and its next time (DST, weekdays, the 31st, every N hours,
cross-checked with systemd-analyze where it exists), the store's `schedule` field and its operations,
one occurrence through the center (not approved, already running, stopped, ended), the systemd user
units (the managed marker, hostile titles, systemctl argv), and the hand-over between the timer's
program, the app and a headless host (claims, stale requests, two triggers at once, a busy store).
Headless: temporary HOME / XDG folders, a fake systemctl and fake agents; no real unit or timer."""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import project_center as pc  # noqa: E402
import project_schedule as ps  # noqa: E402
import schedule_handover as sh  # noqa: E402
import schedule_units as su  # noqa: E402
import test_project_center as tpc  # noqa: E402  (CenterCase: its fakes; no tests are taken from it)
from project_supervisor import ProjectError  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
DAILY = {"kind": "daily", "time": "05:00"}


def at(*parts, zone=BERLIN):
    return dt.datetime(*parts, tzinfo=zone)


class CheckTest(unittest.TestCase):
    def test_canonical_forms(self):
        self.assertIsNone(ps.check(None))
        self.assertEqual(ps.check({"kind": "weekdays", "time": "05:00", "days": ["fri", "mon"]}),
                         {"kind": "weekdays", "time": "05:00", "days": ["mon", "fri"]})
        self.assertEqual(ps.check({"kind": "monthly", "time": "23:59", "day": "last", "until": "2027-02-28"}),
                         {"kind": "monthly", "time": "23:59", "day": "last", "until": "2027-02-28"})
        self.assertEqual(ps.check({"kind": "daily", "time": "05:00", "until": None}), DAILY)

    def test_refused(self):
        bad = [
            {}, [], "daily", {"kind": "yearly", "time": "05:00"}, {"kind": "daily"},
            {"kind": "daily", "time": "5:00"}, {"kind": "daily", "time": "24:00"}, {"kind": "daily", "time": "05:60"},
            {"kind": "daily", "time": "05:00\n"}, {"kind": "daily", "time": "05:00;rm"},
            {"kind": "daily", "time": "05:00", "extra": 1}, {"kind": "daily", "time": "05:00", "days": ["mon"]},
            {"kind": "weekdays", "time": "05:00", "days": []}, {"kind": "weekdays", "time": "05:00", "days": ["mon", "mon"]},
            {"kind": "weekdays", "time": "05:00", "days": ["monday"]}, {"kind": "weekdays", "time": "05:00", "days": "mon"},
            {"kind": "weekly", "time": "05:00", "day": "Mon"}, {"kind": "weekly", "time": "05:00", "day": 1},
            {"kind": "hours", "time": "05:00", "every": 5}, {"kind": "hours", "time": "05:00", "every": True},
            {"kind": "hours", "time": "05:00", "every": 6.0}, {"kind": "hours", "time": "05:00"},
            {"kind": "monthly", "time": "05:00", "day": 0}, {"kind": "monthly", "time": "05:00", "day": 32},
            {"kind": "monthly", "time": "05:00", "day": True}, {"kind": "monthly", "time": "05:00", "day": "first"},
            {"kind": "daily", "time": "05:00", "until": "2026-02-30"}, {"kind": "daily", "time": "05:00", "until": "2026-2-3"},
            {"kind": "daily", "time": "05:00", "until": "1999-12-31"}, {"kind": "daily", "time": "05:00", "until": 20261201},
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ps.check(value)


class NextRunTest(unittest.TestCase):
    def next(self, schedule, after, zone=BERLIN):
        return ps.next_run(ps.check(schedule), after, zone)

    def test_daily_and_weekdays(self):
        self.assertEqual(self.next(DAILY, at(2026, 11, 23, 4, 59)), at(2026, 11, 23, 5, 0))
        self.assertEqual(self.next(DAILY, at(2026, 11, 23, 5, 0)), at(2026, 11, 24, 5, 0))   # strictly after
        work = {"kind": "weekdays", "time": "05:00", "days": ["mon", "tue", "wed", "thu", "fri"]}
        self.assertEqual(self.next(work, at(2026, 11, 27, 6, 0)), at(2026, 11, 30, 5, 0))    # Fri -> Mon
        weekly = {"kind": "weekly", "time": "17:00", "day": "tue"}
        self.assertEqual(self.next(weekly, at(2026, 11, 24, 17, 1)), at(2026, 12, 1, 17, 0))

    def test_every_n_hours_keeps_its_hours(self):
        six = {"kind": "hours", "time": "01:30", "every": 6}
        self.assertEqual(ps.on_calendar(six), "*-*-* 01,07,13,19:30:00")
        self.assertEqual(self.next(six, at(2026, 11, 23, 19, 31)), at(2026, 11, 24, 1, 30))
        late = {"kind": "hours", "time": "22:15", "every": 4}           # anchored at 22: 02,06,…,22
        self.assertEqual(ps.on_calendar(late), "*-*-* 02,06,10,14,18,22:15:00")
        hourly = {"kind": "hours", "time": "00:05", "every": 1}
        self.assertEqual(self.next(hourly, at(2026, 11, 23, 9, 5)), at(2026, 11, 23, 10, 5))

    def test_monthly_31st_and_last_day(self):
        thirty_first = {"kind": "monthly", "time": "05:00", "day": 31}
        self.assertEqual(self.next(thirty_first, at(2026, 1, 31, 6, 0)), at(2026, 3, 31, 5, 0))   # Feb has none
        self.assertEqual(self.next(thirty_first, at(2026, 4, 1, 0, 0)), at(2026, 5, 31, 5, 0))    # nor April
        last = {"kind": "monthly", "time": "05:00", "day": "last"}
        self.assertEqual(self.next(last, at(2027, 2, 1, 0, 0)), at(2027, 2, 28, 5, 0))
        self.assertEqual(self.next(last, at(2028, 2, 1, 0, 0)), at(2028, 2, 29, 5, 0))            # leap year
        self.assertEqual(ps.on_calendar(last), "*-*~01 05:00:00")
        leap = {"kind": "monthly", "time": "05:00", "day": 29}
        self.assertEqual(self.next(leap, at(2027, 1, 30, 0, 0)), at(2027, 3, 29, 5, 0))

    def test_until_is_the_last_day(self):
        schedule = dict(DAILY, until="2026-11-24")
        self.assertEqual(self.next(schedule, at(2026, 11, 23, 6, 0)), at(2026, 11, 24, 5, 0))
        self.assertIsNone(self.next(schedule, at(2026, 11, 24, 5, 0)))
        self.assertTrue(ps.ended(ps.check(schedule), dt.date(2026, 11, 25)))
        self.assertFalse(ps.ended(ps.check(schedule), dt.date(2026, 11, 24)))
        self.assertIsNone(ps.next_run(None, at(2026, 11, 23, 0, 0)))

    def test_summer_time_gap_and_double_hour(self):
        night = {"kind": "daily", "time": "02:30"}
        # 29 March 2026: 02:00-03:00 does not exist in Berlin; systemd does not run that day.
        self.assertEqual(self.next(night, at(2026, 3, 28, 12, 0)), at(2026, 3, 30, 2, 30))
        # 25 October 2026: 02:30 happens twice; it runs once, at the first one (CEST).
        first = self.next(night, at(2026, 10, 24, 12, 0))
        self.assertEqual(first.astimezone(dt.timezone.utc), dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(self.next(night, first).astimezone(dt.timezone.utc),
                         dt.datetime(2026, 10, 26, 1, 30, tzinfo=dt.timezone.utc))
        # Every hour on the short day: 23 runs, on the long day: 24 (02:xx once).
        hourly = ps.check({"kind": "hours", "time": "00:00", "every": 1})
        for day, count in ((dt.date(2026, 3, 29), 23), (dt.date(2026, 10, 25), 24)):
            moment, seen = at(day.year, day.month, day.day, 0, 0) - dt.timedelta(seconds=1), 0
            while True:
                moment = ps.next_run(hourly, moment, BERLIN)
                if moment.date() != day:
                    break
                seen += 1
            self.assertEqual(seen, count, day)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_same_times_as_systemd(self):
        schedules = [DAILY, {"kind": "daily", "time": "02:30"}, {"kind": "weekdays", "time": "07:45", "days": ["mon", "thu"]},
                     {"kind": "weekly", "time": "02:15", "day": "sun"}, {"kind": "hours", "time": "01:30", "every": 6},
                     {"kind": "monthly", "time": "05:00", "day": 31}, {"kind": "monthly", "time": "05:00", "day": "last"}]
        for schedule in schedules:
            for base in (at(2026, 3, 27, 12, 0), at(2026, 10, 24, 12, 0), at(2027, 1, 30, 12, 0)):
                with self.subTest(schedule=schedule, base=base):
                    expected = self.systemd(ps.on_calendar(schedule), base, 3)
                    moment, ours = base, []
                    for _ in range(3):
                        moment = ps.next_run(ps.check(schedule), moment, BERLIN)
                        ours.append(moment.astimezone(dt.timezone.utc).replace(tzinfo=None))
                    self.assertEqual(ours, expected)

    @staticmethod
    def systemd(spec, base, count):
        env = dict(os.environ, TZ="Europe/Berlin")
        done = subprocess.run(["systemd-analyze", "calendar", f"--iterations={count}",
                               f"--base-time={base.strftime('%Y-%m-%d %H:%M:%S')}", spec],
                              capture_output=True, text=True, timeout=20, env=env)
        found = []
        for line in done.stdout.splitlines():
            line = line.strip()
            if line.startswith("(in UTC):"):
                found.append(dt.datetime.strptime(line.split(":", 1)[1].strip()[4:], "%Y-%m-%d %H:%M:%S UTC"))
        return found


class WordsTest(unittest.TestCase):
    def test_summary_lines(self):
        now = at(2026, 11, 23, 12, 0)                 # a Monday
        nxt = at(2026, 11, 24, 5, 0).isoformat()
        self.assertEqual(ps.summary(DAILY, nxt, now), "Every day at 05:00 · next run Tue 05:00")
        self.assertEqual(ps.summary(dict(DAILY, until="2026-12-01"), nxt, now),
                         "Every day at 05:00 · until 1 Dec 2026 · next run Tue 05:00")
        self.assertEqual(ps.summary(dict(DAILY, until="2026-11-01"), None, now),
                         "Every day at 05:00 · until 1 Nov 2026 · ended")
        self.assertEqual(ps.summary(None, None, now), "Not scheduled")
        far = at(2027, 1, 5, 5, 0).isoformat()
        self.assertEqual(ps.summary({"kind": "monthly", "time": "05:00", "day": 31}, far, now),
                         "Monthly on the 31st at 05:00 (months without it are skipped) · next run Tue 5 Jan 2027 05:00")
        texts = {ps.recurrence_text(ps.check(s)) for s in (
            {"kind": "weekdays", "time": "05:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
            {"kind": "weekdays", "time": "05:00", "days": ["sat", "sun"]},
            {"kind": "weekly", "time": "05:00", "day": "wed"},
            {"kind": "hours", "time": "01:30", "every": 6}, {"kind": "hours", "time": "00:15", "every": 1},
            {"kind": "monthly", "time": "05:00", "day": "last"}, {"kind": "monthly", "time": "05:00", "day": 2})}
        self.assertEqual(texts, {"Mon–Fri at 05:00", "Sat and Sun at 05:00", "Every Wednesday at 05:00",
                                 "Every 6 hours from 01:30", "Every hour at :15", "Monthly on the last day at 05:00",
                                 "Monthly on the 2nd at 05:00"})

    def test_notification_is_argv_and_plain(self):
        seen = []
        ps.notify("-t <b>x</b>", "a\nb; $(rm -rf ~) <img src=http://x>",
                  run=lambda argv, **kw: (seen.append((argv, kw)), (b"", True))[1])
        argv, options = seen[0]
        self.assertEqual(argv[:4], ["notify-send", "--app-name=AI Agents Map", "--icon=alarm-symbolic", "--"])
        self.assertEqual(len(argv), 6)
        self.assertNotIn("<", argv[4] + argv[5])
        self.assertNotIn("\n", argv[5])
        self.assertLessEqual(options["timeout"], 5)


class FakeSystemctl:
    """systemctl --user with state: enable --now / restart make a timer enabled and active, disable
    --now ends both, daemon-reload clears "needs reload"; show answers like systemd (one block per
    unit). `fail`: verbs that fail; `forget()`: systemd lost the state (a failed start)."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)
        self.running: set = set()
        self.stale: set = set()                   # names whose files changed without a reload

    def __call__(self, argv, **options):
        assert isinstance(argv, list) and all(isinstance(word, str) for word in argv)
        assert options.get("timeout") and options["timeout"] <= 60
        self.calls.append(argv)
        verb = argv[2]
        if verb in self.fail:
            return b"Failed", False
        if verb == "enable":
            self.running.add(argv[-1])
        elif verb == "restart":
            if argv[-1] not in self.running:
                return b"Failed", False
        elif verb == "disable":
            self.running.discard(argv[-1])
        elif verb == "daemon-reload":
            self.stale.clear()
        elif verb == "show":
            names = [word for word in argv[3:] if word.endswith(".timer")]
            blocks = [f"Id={name}\nActiveState={'active' if name in self.running else 'inactive'}\n"
                      f"UnitFileState={'enabled' if name in self.running else ''}\n"
                      f"NeedDaemonReload={'yes' if name in self.stale else 'no'}" for name in names]
            return "\n\n".join(blocks).encode(), True
        return b"", True

    def verbs(self):
        return [argv[2] for argv in self.calls]


class UnitsTest(unittest.TestCase):
    PID = "0123456789abcdef0123456789abcdef"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "systemd" / "user"
        self.dir.mkdir(parents=True)
        self.here = Path(self.tmp.name) / "plugin"
        self.ctl = FakeSystemctl()

    def sync(self, schedules, **kw):
        return su.sync(schedules, directory=self.dir, run=self.ctl, here=self.here, **kw)

    def test_units_written_marked_and_started(self):
        self.assertEqual(self.sync({self.PID: DAILY}), {})
        timer = (self.dir / f"ai-agents-map-schedule-{self.PID}.timer").read_text()
        service = (self.dir / f"ai-agents-map-schedule-{self.PID}.service").read_text()
        for text in (timer, service):
            self.assertEqual(text.splitlines()[0], su.MARKER)
        self.assertIn("OnCalendar=*-*-* 05:00:00\nPersistent=true\n", timer)
        self.assertIn(f"Unit=ai-agents-map-schedule-{self.PID}.service", timer)
        self.assertIn(f"ExecStart=/usr/bin/python3 -I {self.here}/project_schedule.py run {self.PID}\n", service)
        self.assertIn("Type=oneshot", service)
        name = f"ai-agents-map-schedule-{self.PID}.timer"
        self.assertEqual(self.ctl.calls, [["systemctl", "--user", "daemon-reload"],
                                          ["systemctl", "--user", "enable", "--now", name],
                                          ["systemctl", "--user", "restart", name]])
        self.ctl.calls.clear()
        self.assertEqual(self.sync({self.PID: DAILY}), {})
        self.assertEqual(self.ctl.verbs(), ["show"])              # unchanged and confirmed: only asked
        self.sync({self.PID: {"kind": "weekly", "time": "06:00", "day": "mon"}})
        self.assertIn("OnCalendar=Mon *-*-* 06:00:00", (self.dir / name).read_text())
        self.assertIn(["systemctl", "--user", "restart", name], self.ctl.calls)

    def test_off_removed_and_foreign_units_left_alone(self):
        self.sync({self.PID: DAILY})
        other = "f" * 32
        foreign = self.dir / f"ai-agents-map-schedule-{other}.timer"
        foreign.write_text("[Timer]\nOnCalendar=daily\n")
        self.ctl.calls.clear()
        self.assertEqual(self.sync({}), {})
        self.assertFalse((self.dir / f"ai-agents-map-schedule-{self.PID}.timer").exists())
        self.assertFalse((self.dir / f"ai-agents-map-schedule-{self.PID}.service").exists())
        self.assertTrue(foreign.exists())                         # no marker: never removed
        self.assertIn(["systemctl", "--user", "disable", "--now", f"ai-agents-map-schedule-{self.PID}.timer"],
                      self.ctl.calls)
        self.assertNotIn(f"ai-agents-map-schedule-{other}.timer", sum(self.ctl.calls, []))
        failures = self.sync({other: DAILY})                      # never replaced either
        self.assertIn(other, failures)
        self.assertEqual(foreign.read_text(), "[Timer]\nOnCalendar=daily\n")

    def test_a_link_in_its_place_is_not_followed(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.write_text(su.MARKER + "\n")
        link = self.dir / f"ai-agents-map-schedule-{self.PID}.timer"
        link.symlink_to(target)
        self.assertIn(self.PID, self.sync({self.PID: DAILY}))
        self.assertEqual(target.read_text(), su.MARKER + "\n")
        self.assertFalse(su.managed(link))
        self.sync({})
        self.assertTrue(link.is_symlink())

    def test_ended_schedule_and_remove_all(self):
        ended = dict(DAILY, until="2026-01-01")
        self.assertEqual(self.sync({self.PID: ended}, today=dt.date(2026, 6, 1)), {})
        self.assertEqual(list(self.dir.iterdir()), [])
        self.sync({self.PID: DAILY, "a" * 32: DAILY})
        self.assertEqual(su.remove_all(directory=self.dir, run=self.ctl), {})
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_unsafe_plugin_folder_and_ids(self):
        for folder in ("/home/me/my plugins", "/x/%h", "/x/a;b", "/x/$HOME", "/x/a\nExecStart=/bin/sh", "/x/'q'"):
            with self.subTest(folder=folder):
                self.assertFalse(su.installable(Path(folder)))
                with self.assertRaises(ValueError):
                    su.service_text(self.PID, Path(folder))
        for pid in ("../../evil", "0" * 31, "X" * 32, "0" * 32 + "\n"):
            with self.assertRaises(ValueError):
                su.timer_name(pid)
        failures = self.sync({"../evil": DAILY, self.PID: DAILY})
        self.assertNotIn("../evil", failures)                    # not a project id: never a file name
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         sorted([f"ai-agents-map-schedule-{self.PID}.service", f"ai-agents-map-schedule-{self.PID}.timer"]))

    def test_a_failed_systemctl_is_said(self):
        self.ctl.fail.add("enable")
        self.assertIn(self.PID, self.sync({self.PID: DAILY}))


    def test_a_failed_start_is_retried_by_the_next_sync(self):
        """Codex 2.1.4 r5: a failed enable --now left the files written, the next sync saw them
        unchanged and never started the timer. Now each sync asks systemd about every wanted timer."""
        name = f"ai-agents-map-schedule-{self.PID}.timer"
        self.ctl.fail.add("enable")
        self.assertIn(self.PID, self.sync({self.PID: DAILY}))
        self.assertNotIn(name, self.ctl.running)
        self.ctl.fail.discard("enable")
        self.ctl.calls.clear()
        self.assertEqual(self.sync({self.PID: DAILY}), {})
        self.assertEqual(self.ctl.verbs(), ["show", "daemon-reload", "enable", "restart"])
        self.assertIn(name, self.ctl.running)

    def test_a_failed_state_query_is_never_taken_as_running(self):
        name = f"ai-agents-map-schedule-{self.PID}.timer"
        self.sync({self.PID: DAILY})
        for broken in ("fail", "empty", "other", "cut", "reload"):
            with self.subTest(broken=broken):
                self.ctl.calls.clear()
                answer = {"fail": (b"Failed", False), "empty": (b"", True),
                          "other": (f"Id=x.timer\nActiveState=active\nUnitFileState=enabled\n"
                                    f"NeedDaemonReload=no".encode(), True),
                          "cut": (f"Id={name}\nActiveState=active\nUnitFileState=enabled".encode(), True),
                          "reload": (f"Id={name}\nActiveState=active\nUnitFileState=enabled\n"
                                     f"NeedDaemonReload=yes".encode(), True)}[broken]
                real = self.ctl

                def ctl(argv, **options):
                    result = real(argv, **options)
                    return answer if argv[2] == "show" else result
                self.assertEqual(su.sync({self.PID: DAILY}, directory=self.dir, run=ctl, here=self.here), {})
                self.assertEqual(self.ctl.verbs(), ["show", "daemon-reload", "enable", "restart"])

    def test_many_timers_are_asked_about_in_batches(self):
        ids = [f"{index:032x}" for index in range(su.SHOW_BATCH * 2 + 5)]
        self.sync({pid: DAILY for pid in ids})
        self.ctl.calls.clear()
        self.assertEqual(self.sync({pid: DAILY for pid in ids}), {})
        self.assertEqual(self.ctl.verbs(), ["show", "show", "show"])

    def test_a_timer_that_cannot_be_stopped_keeps_its_files_and_is_said(self):
        """Codex 2.1.0 r1/r2: a failed disable --now was ignored, so clear could report success with a
        timer still active. Now any failed disable keeps the files and is reported; the next sync
        tries again and removes them once systemctl succeeds."""
        timer = self.dir / f"ai-agents-map-schedule-{self.PID}.timer"
        self.sync({self.PID: DAILY})
        self.ctl.fail.add("disable")
        for _attempt in range(2):
            failures = su.remove_all(directory=self.dir, run=self.ctl)
            self.assertIn(self.PID, failures)
            self.assertTrue(timer.exists() and su.managed(timer))
        self.assertNotIn("is-active", sum(self.ctl.calls, []))
        self.ctl.fail.discard("disable")
        self.assertEqual(su.remove_all(directory=self.dir, run=self.ctl), {})
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_the_unit_folder_is_read_one_entry_at_a_time(self):
        """Codex 2.1.2 r3 / 2.1.3 r4: _present streams systemd/user (no listdir) and keeps every id of
        ours, with no cap: 10 005 managed timers are all removed (2.1.3 stopped at 10 000 and clear
        said done with five timers left)."""
        for index in range(200):
            (self.dir / f"other-{index}.service").write_text("")
        ids = [f"{index:032x}" for index in range(10_005)]
        for pid in ids:
            (self.dir / su.timer_name(pid)).write_text(su.MARKER + "\n")
        with mock.patch.object(su.os, "listdir", side_effect=AssertionError("listdir")):
            self.assertEqual(su._present(self.dir), set(ids))
            self.assertEqual(su.remove_all(directory=self.dir, run=self.ctl), {})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith("ai-agents-map")), [])

    def test_remove_all_checks_nothing_of_ours_is_left(self):
        self.sync({self.PID: DAILY})
        with mock.patch.object(su, "sync", return_value={}):          # whatever made a removal miss
            self.assertIn(self.PID, su.remove_all(directory=self.dir, run=self.ctl))
        foreign = self.dir / f"ai-agents-map-schedule-{'f' * 32}.timer"
        foreign.write_text("[Timer]\n")                                # not ours: never counted
        self.assertEqual(su.remove_all(directory=self.dir, run=self.ctl), {})
        self.assertTrue(foreign.exists())

    @unittest.skipIf(os.geteuid() == 0, "root reads any folder")
    def test_an_unreadable_unit_folder_is_a_failure(self):
        self.sync({self.PID: DAILY})
        os.chmod(self.dir, 0o300)
        self.addCleanup(os.chmod, self.dir, 0o755)
        failures = su.remove_all(directory=self.dir, run=self.ctl)
        self.assertIn(su.UNIT_FOLDER, failures)
        with mock.patch("sys.stderr"):
            self.assertEqual(ps.clear(open_store=lambda: type("C", (), {"schedules": lambda s: {},
                                                                        "shutdown": lambda s: None})(),
                                      remove_all=lambda: su.remove_all(directory=self.dir, run=self.ctl)), 1)

    def test_a_failed_reload_after_removing_is_said(self):
        self.sync({self.PID: DAILY})
        self.ctl.fail.add("daemon-reload")
        self.assertIn(self.PID, su.remove_all(directory=self.dir, run=self.ctl))

    def test_clear_removes_the_timers_even_when_turning_a_schedule_off_fails(self):
        """Codex 2.1.4 r5: a failed set_schedule left clear() through `finally` without remove_all, so
        enabled timers stayed. Now every step is tried, every failure is said, and it exits 1."""
        removed = []

        class Center:
            def schedules(self):
                return {"a" * 32: DAILY, "b" * 32: DAILY}

            def call(self, op, params):
                if params["id"] == "a" * 32:
                    raise ProjectError("project_store_write_failed")

            def shutdown(self):
                raise OSError("busy")
        with mock.patch("sys.stderr") as err:
            self.assertEqual(ps.clear(open_store=Center, remove_all=lambda: removed.append(1) or {}), 1)
        said = "".join(str(c.args[0]) for c in err.write.call_args_list)
        self.assertEqual(removed, [1])
        self.assertIn("a" * 32, said)
        self.assertIn("could not be closed", said)
        with mock.patch("sys.stderr"):
            def broken():
                raise ProjectError("project_store_corrupt")
            self.assertEqual(ps.clear(open_store=broken, remove_all=lambda: removed.append(2) or {}), 1)
            self.assertEqual(ps.clear(open_store=Center, remove_all=mock.Mock(side_effect=OSError("x"))), 1)
        self.assertEqual(removed, [1, 2])

    def test_clear_reports_a_timer_left_running(self):
        class Center:
            def schedules(self):
                return {}

            def shutdown(self):
                pass
        with mock.patch("sys.stderr") as err:
            self.assertEqual(ps.clear(open_store=Center, remove_all=lambda: {self.PID: "Failed"}), 1)
        self.assertIn(self.PID, "".join(str(c.args[0]) for c in err.write.call_args_list))


class ScheduleCenterCase(tpc.CenterCase):
    """The real center on a temporary folder with the fixture agent; a recording schedules hook."""

    def setUp(self):
        self.synced = []
        self.timer_failures = {}
        super().setUp()

    def make(self, **kwargs):
        kwargs.setdefault("schedules", self.record)
        return super().make(**kwargs)

    def record(self, schedules):
        self.synced.append(json.loads(json.dumps(schedules)))
        return dict(self.timer_failures)

    def ready(self, **extra):
        return self.approve(self.save(plan="Check the mail.", **extra))

    def schedule(self, pid, value=DAILY):
        return self.center.call("set_schedule", {"id": pid, "schedule": value})


class StoreScheduleTest(ScheduleCenterCase):
    def test_set_schedule_no_revision_and_views(self):
        item = self.ready()
        answer = self.schedule(item["id"], {"kind": "weekdays", "time": "05:00", "days": ["fri", "mon"]})
        project = answer["project"]
        self.assertIsNone(answer["timer_error"])
        self.assertEqual(project["revision"], item["revision"])
        self.assertEqual(project["schedule"], {"kind": "weekdays", "time": "05:00", "days": ["mon", "fri"]})
        self.assertIsNotNone(ps.parse_moment(project["next_run"]))
        self.assertTrue(self.get(item["id"])["approval_valid"])          # a schedule is not content
        self.assertEqual(self.card(item["id"])["schedule"], project["schedule"])
        self.assertEqual(self.card(item["id"])["next_run"], project["next_run"])
        self.assertEqual(self.synced[-1], {item["id"]: project["schedule"]})
        self.assertEqual(self.types(project)[-1], "schedule")
        off = self.schedule(item["id"], None)["project"]
        self.assertIsNone(off["schedule"])
        self.assertIsNone(off["next_run"])
        self.assertEqual(self.synced[-1], {})
        stored = json.loads((self.root / "ai-project-center" / "projects.json").read_text())
        self.assertNotIn("schedule", stored["projects"][0])

    def test_refused_requests(self):
        item = self.save()
        for params in ({"id": item["id"]}, {"id": item["id"], "schedule": {"kind": "daily", "time": "25:00"}},
                       {"id": item["id"], "schedule": DAILY, "revision": 1}):
            self.assertCode("invalid", self.center.call, "set_schedule", params)
        self.assertCode("project_unknown", self.center.call, "set_schedule", {"id": "0" * 32, "schedule": DAILY})

    def test_reopened_old_store_and_corrupt_schedule(self):
        item = self.save()
        self.schedule(item["id"])
        self.center.detach()
        reopened = self.make()
        self.assertEqual(reopened.call("get", {"id": item["id"]})["project"]["schedule"], DAILY)
        self.assertEqual(self.synced[-1], {item["id"]: DAILY})            # the timers follow on opening
        reopened.detach()
        store = self.root / "ai-project-center" / "projects.json"
        data = json.loads(store.read_text())
        del data["projects"][0]["schedule"]                               # a store from before schedules
        store.write_text(json.dumps(data))
        old = self.make()
        self.assertIsNone(old.call("get", {"id": item["id"]})["project"]["schedule"])
        old.detach()
        for broken in ({"kind": "daily", "time": "05:00\nExecStart=/bin/sh"},
                       {"kind": "weekdays", "time": "05:00", "days": ["fri", "mon"]}):    # not canonical
            data["projects"][0]["schedule"] = broken
            store.write_text(json.dumps(data))
            self.assertCode("project_store_corrupt", self.make)

    def test_delete_removes_the_timer_and_failures_are_said(self):
        item = self.save()
        self.timer_failures = {item["id"]: "systemctl could not start the timer"}
        self.assertEqual(self.schedule(item["id"])["timer_error"], "systemctl could not start the timer")
        self.timer_failures = {}
        current = self.get(item["id"])
        self.center.call("delete", {"id": item["id"], "revision": current["revision"]})
        self.assertEqual(self.synced[-1], {})

    def test_hostile_title_never_reaches_a_unit(self):
        title = "Mail\n[Service]\nExecStart=/bin/sh -c 'rm -rf ~'; $(id) %h `x`"
        item = self.save(title=title)
        self.schedule(item["id"])
        with tempfile.TemporaryDirectory() as folder:
            directory, ctl = Path(folder), FakeSystemctl()
            su.sync(self.center.schedules(), directory=directory, run=ctl, here=Path("/opt/plugin"))
            written = "".join(p.read_text() for p in directory.iterdir())
            for piece in ("rm -rf", "/bin/sh", "$(id)", "Mail", "%h", "`"):
                self.assertNotIn(piece, written)
            self.assertNotIn(title, json.dumps(ctl.calls))
            for argv in ctl.calls:
                self.assertTrue(all("\n" not in word and ";" not in word for word in argv))

    def test_without_a_hook_no_timer_is_touched(self):
        center_dir = tempfile.TemporaryDirectory()
        self.addCleanup(center_dir.cleanup)
        with mock.patch.object(su, "sync", side_effect=AssertionError("touched")):
            center = pc.ProjectCenter(center_dir.name, agent_profiles=lambda: self.profiles,
                                      runtime_profiles=lambda: [], authorize=lambda agent: None)
            self.assertEqual(center.sync_schedules(), {})
            center.shutdown()

    def test_schedule_unavailable_when_the_plugin_folder_is_unsafe(self):
        item = self.save()
        with mock.patch.object(su, "installable", return_value=False):
            self.assertCode("project_schedule_unavailable", self.schedule, item["id"])
            self.schedule(item["id"], None)                               # off is always possible

    def test_note_is_coalesced(self):
        item = self.save()
        first = self.center.call("schedule_note", {"id": item["id"], "reason": "running", "text": "Skipped."})
        again = self.center.call("schedule_note", {"id": item["id"], "reason": "running", "text": "Skipped."})
        self.assertFalse(first["repeated"])
        self.assertTrue(again["repeated"])
        self.assertEqual(self.types(again["project"]).count("schedule_running"), 1)
        self.assertEqual(again["project"]["revision"], item["revision"])
        self.assertCode("invalid", self.center.call, "schedule_note", {"id": item["id"], "reason": "x", "text": "y"})
        self.assertCode("invalid", self.center.call, "schedule_note",
                        {"id": item["id"], "reason": "running", "text": "y", "more": 1})


class ScheduledRunTest(ScheduleCenterCase):
    def setUp(self):
        super().setUp()
        self.notes = []

    def notify(self, title, body):
        self.notes.append((title, body))

    def trigger(self, pid, today=None):
        return ps.trigger(self.center.call, pid, self.notify, today=today)

    def test_scheduled_run_takes_the_manual_path_and_runs_again_after_completion(self):
        item = self.ready()
        self.schedule(item["id"])
        self.assertEqual(self.trigger(item["id"]), "started")
        tpc.wait_until(lambda: len(self.processes) == 1)
        self.assertEqual(self.commands[0][:2], ["/fixture/agent", "workspace-write"])
        self.assertIn("Scheduled run: executing the approved plan.", [e["text"] for e in self.get(item["id"])["events"]])
        self.processes[0].finish("done", 0)
        self.idle()
        self.assertEqual(self.get(item["id"])["status"], "completed")
        # A manual Run from Completed still needs approval again; the schedule's next occurrence does not.
        current = self.get(item["id"])
        self.assertCode("project_plan_not_approved", self.center.call, "run",
                        {"id": item["id"], "revision": current["revision"]})
        self.assertEqual(self.trigger(item["id"]), "started")
        tpc.wait_until(lambda: len(self.processes) == 2)
        self.assertEqual(self.notes, [])

    def test_rules_are_asked_as_for_a_manual_run(self):
        item = self.ready()
        self.schedule(item["id"])
        self.refused_agents.add("fixture")
        self.assertEqual(self.trigger(item["id"]), "started")
        self.idle()
        self.assertEqual(self.processes, [])
        self.assertEqual(self.get(item["id"])["status"], "failed")

    def test_not_approved_skips_records_and_notifies_once(self):
        item = self.ready()
        self.schedule(item["id"])
        current = self.get(item["id"])
        self.center.call("save", {"id": item["id"], "revision": current["revision"], "plan": "Another plan."})
        self.assertEqual(self.trigger(item["id"]), "not_approved")
        self.assertEqual(self.trigger(item["id"]), "not_approved")
        self.assertEqual(self.processes, [])
        self.assertEqual(len(self.notes), 1)
        self.assertIn("scheduled run skipped", self.notes[0][0])
        self.assertEqual(self.types(self.get(item["id"])).count("schedule_not_approved"), 1)

    def test_team_changed_since_approval(self):
        item = self.ready()
        self.schedule(item["id"])
        self.profiles[0]["run_argv"] = ["/fixture/agent", "other-mode", "{prompt}"]   # launches differently now
        self.center.bump_discovery()
        self.assertEqual(self.trigger(item["id"]), "not_approved")
        self.assertEqual(self.processes, [])
        self.assertEqual(len(self.notes), 1)

    def test_already_running_is_recorded_without_a_notification(self):
        item = self.ready()
        self.schedule(item["id"])
        self.assertEqual(self.trigger(item["id"]), "started")
        tpc.wait_until(lambda: len(self.processes) == 1)
        self.assertEqual(self.trigger(item["id"]), "running")
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.notes, [])
        self.assertIn("schedule_running", self.types(self.get(item["id"])))
        self.processes[0].finish("", 0)

    def test_stopped_run_and_unscheduled_and_ended(self):
        item = self.ready()
        self.assertEqual(self.trigger(item["id"]), "unscheduled")                 # a stale timer: nothing
        current = self.get(item["id"])
        self.assertCode("project_not_scheduled", self.center.call, "run",
                        {"id": item["id"], "revision": current["revision"], "scheduled": True})
        self.assertCode("invalid", self.center.call, "run",
                        {"id": item["id"], "revision": current["revision"], "scheduled": "yes"})
        self.schedule(item["id"], dict(DAILY, until="2026-01-01"))
        self.assertEqual(self.trigger(item["id"], today=dt.date(2026, 6, 1)), "ended")
        self.assertEqual(self.notes, [])
        self.schedule(item["id"])
        self.assertEqual(self.trigger(item["id"]), "started")
        tpc.wait_until(lambda: len(self.processes) == 1)
        self.processes[0].finish("", 3)                                            # the agent failed
        self.idle()
        self.assertEqual(self.get(item["id"])["status"], "failed")
        self.assertEqual(self.trigger(item["id"]), "review_changes")      # its changes are looked at first
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.trigger("0" * 32), "unknown")


    def test_a_refused_start_is_recorded_and_notified(self):
        first = self.ready(title="First")
        self.center.call("run", {"id": first["id"], "revision": first["revision"]})
        tpc.wait_until(lambda: len(self.processes) == 1)
        second = self.ready(title="Second")                          # the same folder
        self.schedule(second["id"])
        self.assertEqual(self.trigger(second["id"]), "failed")
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.notes[0][0], "Second: scheduled run failed")
        self.assertIn("Another project is working in this folder.", self.notes[0][1])
        self.assertIn("schedule_failed", self.types(self.get(second["id"])))
        self.processes[0].finish("", 0)

    def test_blocked_by_status(self):
        base = {"schedule": DAILY, "status": "ready", "approved_version": 1, "plan_version": 1}
        today = dt.date(2026, 11, 23)
        for change, reason in (({}, None), ({"status": "completed"}, None), ({"status": "running"}, "running"),
                               ({"status": "pausing"}, "running"), ({"processes_stopping": True}, "running"),
                               ({"status": "paused"}, "stopped"), ({"status": "interrupted"}, "stopped"),
                               ({"status": "failed", "needs_reconciliation": True}, "review_changes"),
                               ({"status": "awaiting_review"}, "not_approved"), ({"status": "draft"}, "not_approved"),
                               ({"approved_version": None}, "not_approved"), ({"plan_version": 2}, "not_approved"),
                               ({"schedule": None}, "unscheduled"),
                               ({"schedule": dict(DAILY, until="2026-11-22")}, "ended")):
            with self.subTest(change=change):
                self.assertEqual(ps._blocked({**base, **change}, today), reason)


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "run" / sh.QUEUE_NAME
        self.queue.parent.mkdir(mode=0o700)


class QueueTest(QueueCase):
    PID = "0123456789abcdef0123456789abcdef"

    def test_claim_answer_and_withdraw(self):
        nonce = sh.submit(self.PID, self.queue)
        self.assertEqual(os.stat(self.queue).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.queue / f"{nonce}.req").st_mode & 0o777, 0o600)
        self.assertTrue(sh.waiting(self.queue) and sh.pending(nonce, self.queue))
        self.assertTrue(sh.withdraw(nonce, self.queue))
        self.assertFalse(sh.pending(nonce, self.queue) or sh.waiting(self.queue))
        self.assertIsNone(sh.answer(nonce, self.queue))
        with self.assertRaises(ValueError):
            sh.submit("../../etc", self.queue)

    def test_serve_runs_through_the_owner(self):
        nonce = sh.submit(self.PID, self.queue)
        with mock.patch.object(ps, "trigger", side_effect=lambda call, pid, notify: (call, pid)[0] and "started"):
            served = sh.serve(lambda: "the call", lambda *a: None, self.queue)
        self.assertEqual(served, [(nonce, "started")])
        self.assertFalse(sh.pending(nonce, self.queue))
        self.assertEqual(sh.answer(nonce, self.queue), "started")
        self.assertIsNone(sh.answer(nonce, self.queue))      # read once
        self.assertFalse(sh.withdraw(nonce, self.queue))

    def test_not_the_owner_claims_nothing(self):
        nonce = sh.submit(self.PID, self.queue)

        def busy():
            raise ProjectError("project_center_busy")
        with mock.patch.object(ps, "trigger", side_effect=AssertionError("ran")):
            self.assertEqual(sh.serve(busy, lambda *a: None, self.queue), [])
        self.assertTrue(sh.pending(nonce, self.queue))
        self.assertTrue(sh.withdraw(nonce, self.queue))
        self.assertFalse(sh.pending(nonce, self.queue))

    def test_hostile_and_stale_requests_are_not_run(self):
        sh._open_queue(self.queue)
        names = {}
        for label, body in (("stale", {"project": self.PID, "at": time.time() - 3600}),
                            ("future", {"project": self.PID, "at": time.time() + 3600}),
                            ("badid", {"project": "../../x", "at": time.time()}),
                            ("extra", {"project": self.PID, "at": time.time(), "argv": ["sh"]}),
                            ("bool", {"project": self.PID, "at": True})):
            nonce = os.urandom(16).hex()
            (self.queue / f"{nonce}.req").write_text(json.dumps(body))
            names[nonce] = label
        big = os.urandom(16).hex()
        (self.queue / f"{big}.req").write_text(json.dumps({"project": self.PID, "at": time.time(), "x": "y" * 4000}))
        link = os.urandom(16).hex()
        (Path(self.tmp.name) / "target").write_text(json.dumps({"project": self.PID, "at": time.time()}))
        (self.queue / f"{link}.req").symlink_to(Path(self.tmp.name) / "target")
        (self.queue / "notanonce.req").write_text(json.dumps({"project": self.PID, "at": time.time()}))
        with mock.patch.object(ps, "trigger", side_effect=AssertionError("ran")):
            served = dict(sh.serve(lambda: "call", lambda *a: None, self.queue))
        self.assertEqual({served[n] for n in names if names[n] == "stale"}, {"stale"})
        self.assertEqual({served[n] for n in names if names[n] == "future"}, {"stale"})
        self.assertEqual({served[n] for n in names if names[n] in ("badid", "extra", "bool")}, {"invalid"})
        self.assertEqual(served[big], "invalid")
        self.assertEqual(served[link], "invalid")                 # a link is not followed
        self.assertNotIn("notanonce", served)

    def test_a_request_is_never_seen_half_written(self):
        """Codex 2.1.0 r1: the .req file appeared before its JSON was written, so an owner claiming it
        then read nothing and the occurrence was lost. Now it appears only complete (requests and
        answers alike), also when the system writes it in pieces."""
        real_write, seen = os.write, []

        def one_byte(fd, data):
            seen.append(sorted(p.name for p in self.queue.iterdir() if not p.name.endswith(".tmp")))
            return real_write(fd, bytes(data[:1]))
        with mock.patch.object(sh.os, "write", side_effect=one_byte):
            nonce = sh.submit(self.PID, self.queue)
        self.assertGreater(len(seen), 10)
        self.assertTrue(all(names == [] for names in seen))           # nothing visible while writing
        self.assertEqual(sorted(p.name for p in self.queue.iterdir()), [f"{nonce}.req"])
        self.assertEqual(os.stat(self.queue / f"{nonce}.req").st_mode & 0o777, 0o600)
        with mock.patch.object(ps, "trigger", return_value="started"):
            self.assertEqual(sh.serve(lambda: "call", lambda *a: None, self.queue), [(nonce, "started")])
        self.assertEqual(sh.answer(nonce, self.queue), "started")

    def test_a_short_write_leaves_nothing(self):
        with mock.patch.object(sh.os, "write", return_value=0):
            with self.assertRaises(OSError):
                sh.submit(self.PID, self.queue)
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_an_existing_name_is_not_replaced_and_leftovers_go(self):
        fd = sh._open_queue(self.queue)
        try:
            sh._create(fd, "a" * 32 + ".ans", {"outcome": "started"})
            with self.assertRaises(FileExistsError):
                sh._create(fd, "a" * 32 + ".ans", {"outcome": "failed"})
            leftover = self.queue / ("b" * 32 + ".req.0011223344556677.tmp")
            leftover.write_text("{")
            old = time.time() - sh.LEFTOVER_AGE - 60
            os.utime(leftover, (old, old))
            sh._clean(fd, time.time())
        finally:
            os.close(fd)
        self.assertFalse(leftover.exists())
        self.assertEqual(sh.answer("a" * 32, self.queue), "started")

    def test_many_other_files_neither_hide_a_request_nor_stall_the_check(self):
        """Codex 2.1.0 r1/r2: the listing is gone through one entry at a time (bounded memory), a
        request behind many other files is still served, and the main loop's check stops early."""
        sh._open_queue(self.queue)
        for index in range(300):                                 # before and after it, whatever the
            (self.queue / f"junk{index:04d}").write_text("")     # file system's listing order
            if index == 150:
                nonce = sh.submit(self.PID, self.queue)
        with mock.patch.object(sh, "MAX_LISTED", 10), mock.patch.object(sh.os, "scandir", wraps=os.scandir):
            self.assertTrue(sh.waiting(self.queue))               # more than it looks at: a worker looks
            with mock.patch.object(ps, "trigger", return_value="started"):
                served = sh.serve(lambda: "call", lambda *a: None, self.queue)
        self.assertEqual(served, [(nonce, "started")])
        for index in range(sh.MAX_REQUESTS + 3):                  # more than one pass's worth
            sh.submit(self.PID, self.queue)
        with mock.patch.object(ps, "trigger", return_value="started"):
            first = sh.serve(lambda: "call", lambda *a: None, self.queue)
            second = sh.serve(lambda: "call", lambda *a: None, self.queue)
        self.assertEqual((len(first), len(second)), (sh.MAX_REQUESTS, 3))
        self.assertFalse(sh.waiting(self.queue, whole=True))      # the host: only real requests count
        for path in self.queue.glob("junk*"):
            path.unlink()
        for path in self.queue.glob("*.ans"):
            path.unlink()
        self.assertFalse(sh.waiting(self.queue))

    def test_a_queue_others_can_enter_is_refused(self):
        self.queue.mkdir(mode=0o700)
        os.chmod(self.queue, 0o733)
        with self.assertRaises(OSError):
            sh.submit(self.PID, self.queue)
        os.chmod(self.queue, 0o700)
        os.rmdir(self.queue)
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.queue.symlink_to(elsewhere)
        with self.assertRaises(OSError):
            sh.submit(self.PID, self.queue)

    def test_app_running_checks_identity(self):
        runtime = Path(self.tmp.name) / "run"
        script_dir = Path(self.tmp.name) / "plugin"
        script_dir.mkdir()
        (script_dir / "ai_popup.py").write_text("import time\ntime.sleep(30)\n")
        python = os.path.realpath(sys.executable)
        child = subprocess.Popen([python, str(script_dir / "ai_popup.py")])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        import pidfile
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            self.assertFalse(sh.app_running(python=python, here=script_dir))           # no PID file
            tpc.wait_until(lambda: pidfile.process_starttime(child.pid) != "")
            pidfile.write(child.pid)
            self.assertTrue(sh.app_running(python=python, here=script_dir))
            self.assertFalse(sh.app_running(python=python, here=Path(self.tmp.name)))  # another script
            self.assertFalse(sh.app_running(python="/bin/sh", here=script_dir))        # another interpreter
            (runtime / pidfile.PID_NAME).write_text(f"{child.pid}:1\n")                 # a reused PID
            self.assertFalse(sh.app_running(python=python, here=script_dir))
            (runtime / pidfile.PID_NAME).write_text(f"{child.pid}\n")                   # no start time
            self.assertFalse(sh.app_running(python=python, here=script_dir))


class FakeHostCenter:
    """Stands in for ProjectCenter in the job: records runs, works for `work` looks."""

    def __init__(self, work=0):
        self.calls, self.work, self.released = [], work, False

    def call(self, operation, params):
        self.calls.append(operation)
        return {}

    def working(self):
        self.work -= 1
        return self.work >= 0

    def shutdown(self):
        self.released = True

    def detach(self):
        self.released = True


class JobTest(QueueCase):
    PID = "0123456789abcdef0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.notes = []
        patcher = mock.patch.object(sh, "HOST_LOOK", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, open_store, app=False, deadline=sh.JOB_DEADLINE, **kw):
        return sh.job(self.PID, open_store=open_store, notify=lambda *a: self.notes.append(a),
                      app=lambda: app, directory=self.queue, sleep=lambda s: time.sleep(min(s, 0.01)),
                      deadline=deadline, **kw)

    def test_nobody_holds_the_store_the_job_hosts_it(self):
        center = FakeHostCenter(work=3)
        with mock.patch.object(ps, "trigger", return_value="started") as trigger:
            self.assertEqual(self.job(lambda: center), 0)
        self.assertEqual(trigger.call_count, 1)
        self.assertEqual(trigger.call_args[0][1], self.PID)
        self.assertTrue(center.released and center.work < 0)      # waited until no project worked
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_the_app_takes_it_first(self):
        opened = []

        def app_serves():
            tpc.wait_until(lambda: sh.waiting(self.queue))
            with mock.patch.object(ps, "trigger", return_value="started"):
                sh.serve(lambda: "app call", lambda *a: None, self.queue)
        thread = threading.Thread(target=app_serves)
        thread.start()
        self.assertEqual(self.job(lambda: opened.append(1), app=True), 0)
        thread.join(5)
        self.assertEqual(opened, [])                                  # never opened the store itself

    def test_a_busy_store_is_given_up_with_a_notification(self):
        def busy():
            raise ProjectError("project_center_busy")
        self.assertEqual(self.job(busy, deadline=0.2), 1)
        self.assertEqual(len(self.notes), 1)
        self.assertFalse(sh.waiting(self.queue))                     # withdrawn

    def test_a_broken_store_is_said(self):
        def broken():
            raise ProjectError("project_store_corrupt")
        self.assertEqual(self.job(broken), 1)
        self.assertIn("saved project store", self.notes[0][1])

    def test_its_own_occurrence_is_served_before_the_timers_are_reconciled(self):
        """Codex 2.1.4 r5: the timer job opened the store, which synced every unit (30 s per slow
        systemctl) before it served its own request, so the request could go stale. Now the store is
        opened without a sync, the own request is claimed and run first, and the reconciliation runs
        after it; the store is let go only once it ended."""
        order = []

        class Center(FakeHostCenter):
            def sync_schedules(self):
                order.append("sync start")
                time.sleep(0.3)
                order.append("sync end")
                return {}

            def shutdown(self):
                order.append("release")
                super().shutdown()
        center = Center(work=0)

        def trigger(call, project_id, notify):
            order.append("run")
            return "started"
        with mock.patch.object(ps, "trigger", side_effect=trigger), \
                mock.patch.object(sh, "REQUEST_AGE", 0.2):         # would be stale after the slow sync
            self.assertEqual(self.job(lambda: center), 0)
        self.assertEqual(order, ["run", "sync start", "sync end", "release"])

    def test_the_timer_program_opens_the_store_without_a_sync(self):
        with mock.patch("project_center.ProjectCenter") as opened:
            sh._open_store()
        self.assertIs(opened.call_args.kwargs["sync_at_open"], False)

    def test_a_host_serves_another_timer_meanwhile(self):
        center = FakeHostCenter(work=400)
        first_done = threading.Event()
        results = []

        def host_job():
            with mock.patch.object(ps, "trigger", return_value="started"):
                results.append(self.job(lambda: center))
            first_done.set()
        thread = threading.Thread(target=host_job)
        thread.start()
        tpc.wait_until(lambda: center.work < 398)                     # the first job hosts the store now

        def busy():
            raise ProjectError("project_center_busy")
        second = sh.job("f" * 32, open_store=busy, notify=lambda *a: self.notes.append(a), app=lambda: False,
                        directory=self.queue, sleep=lambda s: time.sleep(min(s, 0.01)))
        thread.join(10)
        self.assertEqual((second, results), (0, [0]))
        self.assertEqual(self.notes, [])


class TwoTriggersTest(ScheduleCenterCase):
    """Two timer programs at once on the real store: one host, one run, the other occurrence skipped."""

    def test_overlapping_triggers_start_one_run(self):
        item = self.ready()
        self.schedule(item["id"])
        self.center.detach()
        queue = self.root / "run" / sh.QUEUE_NAME
        queue.parent.mkdir(mode=0o700)
        centers = []

        def open_store():
            center = self.make()           # the same folder: its lock decides
            centers.append(center)
            return center
        results, notes = [], []

        def one():
            results.append(sh.job(item["id"], open_store=open_store, notify=lambda *a: notes.append(a),
                                  app=lambda: False, directory=queue, sleep=lambda s: time.sleep(min(s, 0.02))))
        with mock.patch.object(sh, "HOST_LOOK", 0.02):
            threads = [threading.Thread(target=one) for _ in range(2)]
            for thread in threads:
                thread.start()
            tpc.wait_until(lambda: len(self.processes) == 1, 20)
            time.sleep(0.3)
            self.assertEqual(len(self.processes), 1)
            self.processes[0].finish("", 0)
            for thread in threads:
                thread.join(30)
        self.assertEqual(sorted(results), [0, 0])
        self.assertEqual(len(centers), 1)
        reopened = self.make()
        events = [e["type"] for e in reopened.call("get", {"id": item["id"]})["project"]["events"]]
        self.assertIn("schedule_running", events)


class ProgramTest(unittest.TestCase):
    def test_entry_point_refuses_other_words(self):
        script = str(ROOT / "project_schedule.py")
        for argv in ([], ["run"], ["run", "../x"], ["run", "0" * 32, "extra"], ["serve"]):
            done = subprocess.run([sys.executable, "-I", script, *argv], capture_output=True, timeout=30,
                                  env={"PATH": "/usr/bin", "HOME": "/nonexistent"})
            self.assertEqual(done.returncode, 2, argv)

    def test_the_timer_program_end_to_end_in_a_temporary_home(self):
        """`python3 -I project_schedule.py run <id>` as the timer starts it: a temporary HOME and XDG
        folders, fake systemctl and notify-send first on PATH; nothing of this account is touched."""
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            runtime, fakes, log = home / "run", home / "bin", home / "calls.log"
            runtime.mkdir(mode=0o700)
            fakes.mkdir()
            for name in ("systemctl", "notify-send"):
                fake = fakes / name
                fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"{name} $*\" >> {log}\n")
                fake.chmod(0o755)
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_RUNTIME_DIR": str(runtime),
                   "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
                   "PATH": f"{fakes}:/usr/bin:/bin"}
            config = home / ".config" / "omarchy" / "ai-agents-map"
            workspace = home / "work"
            workspace.mkdir()
            center = pc.ProjectCenter(config, agent_profiles=lambda: [], runtime_profiles=lambda: [],
                                      authorize=lambda agent: None)
            item = center.call("save", {"title": "Mail\n[Service]; rm -rf ~", "brief": "Check the mail.",
                                        "path": str(workspace), "team": [], "plan": ""})["project"]
            center.call("set_schedule", {"id": item["id"], "schedule": DAILY})
            center.shutdown()
            done = subprocess.run([sys.executable, "-I", str(ROOT / "project_schedule.py"), "run", item["id"]],
                                  env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            calls = log.read_text().splitlines()
            timer = f"ai-agents-map-schedule-{item['id']}.timer"
            self.assertIn("systemctl --user daemon-reload", calls)
            self.assertIn(f"systemctl --user enable --now {timer}", calls)
            self.assertTrue(any(c.startswith("notify-send --app-name=AI Agents Map") and "skipped" in c for c in calls),
                            calls)
            units = home / ".config" / "systemd" / "user"
            self.assertEqual(sorted(p.name for p in units.iterdir()), sorted([timer, timer[:-6] + ".service"]))
            self.assertNotIn("rm -rf", "".join(p.read_text() for p in units.iterdir()))
            store = json.loads((config / "ai-project-center" / "projects.json").read_text())
            self.assertEqual(store["projects"][0]["events"][-1]["type"], "schedule_not_approved")
            self.assertEqual(list((runtime / sh.QUEUE_NAME).iterdir()), [])       # nothing left in the queue

    def test_clear_turns_schedules_off_and_removes_timers(self):
        removed = []

        class Center:
            def __init__(self):
                self.calls = []

            def schedules(self):
                return {"a" * 32: DAILY}

            def call(self, op, params):
                self.calls.append((op, params))

            def shutdown(self):
                self.down = True
        center = Center()
        self.assertEqual(ps.clear(open_store=lambda: center, remove_all=lambda: removed.append(1) or {}), 0)
        self.assertEqual(center.calls, [("set_schedule", {"id": "a" * 32, "schedule": None})])
        self.assertEqual(removed, [1])

        def busy():
            raise ProjectError("project_center_busy")
        with mock.patch("sys.stderr"):
            self.assertEqual(ps.clear(open_store=busy, remove_all=lambda: {}), 0)


class ControllerScheduleTest(unittest.TestCase):
    """projects_controller.set_schedule over the board's FakeCenter (no display)."""

    def test_set_schedule_takes_the_answer(self):
        from test_projects_ui import FakeCenter, card
        from projects_controller import ProjectsController
        center = FakeCenter([card("p1")])
        answers = {"project": dict(center.view("p1"), schedule=DAILY, next_run="2026-11-24T05:00+01:00"),
                   "timer_error": None}
        center.results["set_schedule"] = answers
        jobs = []
        ctrl = ProjectsController(center.call, lambda work, done: jobs.append((work, done)), lambda *a: None)
        ctrl.open, ctrl.project = True, center.view("p1")
        finished = []
        ctrl.set_schedule("p1", DAILY, finished.append)
        self.assertIn("p1", ctrl.scheduling)
        work, done = jobs.pop(0)
        done(work(), None)
        self.assertEqual(finished, [True])
        self.assertEqual(ctrl.project["schedule"], DAILY)
        self.assertNotIn("p1", ctrl.scheduling)
        self.assertIn(("set_schedule", {"id": "p1", "schedule": DAILY}), center.calls)
        center.results["set_schedule"] = dict(answers, timer_error="systemctl failed")
        ctrl.set_schedule("p1", DAILY)
        jobs = [job for job in jobs if job[0] is not None]
        work, done = jobs.pop(-1)
        done(work(), None)
        self.assertIn("timer could not be set up", ctrl.error)

    def test_texts_for_the_card_and_page(self):
        import project_texts as texts
        tomorrow = (dt.datetime.now().astimezone() + dt.timedelta(days=1)).replace(hour=5, minute=0, second=0,
                                                                                  microsecond=0)
        project = {"schedule": DAILY, "next_run": tomorrow.isoformat(timespec="minutes")}
        self.assertEqual(texts.schedule_next(project), tomorrow.strftime("%a 05:00"))
        self.assertTrue(texts.schedule_summary(project).startswith("Every day at 05:00 · next run "))
        self.assertEqual(texts.schedule_next({"schedule": DAILY, "next_run": None}), "Schedule ended")
        self.assertIsNone(texts.schedule_next({"schedule": None}))
        self.assertEqual(texts.schedule_summary({"schedule": {"kind": "bogus"}}), "Not scheduled")


if __name__ == "__main__":
    unittest.main()
