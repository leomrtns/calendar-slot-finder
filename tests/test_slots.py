from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from slot_finder import BusyEvent, load_events, rank_slots

LONDON = ZoneInfo('Europe/London')
UTC = timezone.utc
START, END = date(2026, 9, 21), date(2026, 10, 18)


def calendar(tmp_path, *bodies, header=''):
  text = 'BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//EN\n' + header
  for body in bodies:
    text += 'BEGIN:VEVENT\n' + body.strip() + '\nEND:VEVENT\n'
  path = tmp_path / 'calendar.ics'
  path.write_text(text + 'END:VCALENDAR\n')
  return path


def load(path, **kwargs):
  return load_events([path], START, END, LONDON, **kwargs)[0]


def test_recurrence_exclusions_additions_and_moved_instance(tmp_path):
  path = calendar(tmp_path, '''UID:series
DTSTART;TZID=Europe/London:20260921T090000
DTEND;TZID=Europe/London:20260921T100000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=Europe/London:20260928T090000
RDATE;TZID=Europe/London:20260922T090000
SUMMARY:Lecture''', '''UID:series
RECURRENCE-ID;TZID=Europe/London:20261005T090000
DTSTART;TZID=Europe/London:20261006T110000
DTEND;TZID=Europe/London:20261006T120000
SUMMARY:Moved''')
  events = load(path)
  assert [e.start.astimezone(LONDON).strftime('%m-%d %H:%M') for e in events] == [
    '09-21 09:00', '09-22 09:00', '10-06 11:00', '10-12 09:00']


@pytest.mark.parametrize('exception', ['STATUS:CANCELLED', 'TRANSP:TRANSPARENT', 'X-MICROSOFT-CDO-BUSYSTATUS:FREE'])
def test_exception_replaces_busy_master(tmp_path, exception):
  path = calendar(tmp_path, '''UID:series
DTSTART:20260921T090000Z
DTEND:20260921T100000Z
RRULE:FREQ=WEEKLY;COUNT=2''', f'''UID:series
RECURRENCE-ID:20260928T090000Z
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
{exception}''')
  assert len(load(path)) == 1


@pytest.mark.parametrize('property', ['TRANSP:TRANSPARENT', 'X-MICROSOFT-CDO-BUSYSTATUS:FREE', 'STATUS:CANCELLED'])
def test_nonblocking_events(tmp_path, property):
  path = calendar(tmp_path, f'UID:x\nDTSTART:20260921T090000Z\nDTEND:20260921T100000Z\n{property}')
  assert load(path) == []


@pytest.mark.parametrize('property', ['STATUS:TENTATIVE', 'X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE'])
def test_tentative_options(tmp_path, property):
  path = calendar(tmp_path, f'UID:x\nDTSTART:20260921T090000Z\nDURATION:PT1H\n{property}')
  assert load(path)[0].tentative
  assert load(path, tentative='ignore') == []


def test_all_day_exclusive_end_and_default_duration(tmp_path):
  path = calendar(tmp_path, 'UID:x\nDTSTART;VALUE=DATE:20260921\nDTEND;VALUE=DATE:20260923',
                  'UID:y\nDTSTART;VALUE=DATE:20260925')
  events = load(path)
  assert (events[0].end - events[0].start).days == 2
  assert events[1].end - events[1].start == timedelta(days=1)
  assert load(path, all_day='ignore') == []


def test_floating_calendar_timezone_and_override(tmp_path):
  path = calendar(tmp_path, 'UID:x\nDTSTART:20260921T090000\nDURATION:PT1H',
                  header='X-WR-TIMEZONE:America/New_York\n')
  assert load(path)[0].start.hour == 13
  assert load(path, floating_zone=LONDON)[0].start.hour == 8


def test_dst_recurrence_keeps_local_time(tmp_path):
  path = calendar(tmp_path, '''UID:x
DTSTART;TZID=Europe/London:20261019T090000
DTEND;TZID=Europe/London:20261019T100000
RRULE:FREQ=WEEKLY;COUNT=3''')
  events, _ = load_events([path], date(2026, 10, 19), date(2026, 11, 2), LONDON)
  assert [event.start.hour for event in events] == [8, 9, 9]
  assert all(event.start.astimezone(LONDON).hour == 9 for event in events)


def test_embedded_vtimezone(tmp_path):
  header = '''BEGIN:VTIMEZONE
TZID:Custom/Fixed
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0200
TZOFFSETTO:+0200
END:STANDARD
END:VTIMEZONE
'''
  path = calendar(tmp_path, 'UID:x\nDTSTART;TZID=Custom/Fixed:20260921T090000\nDURATION:PT1H', header=header)
  assert load(path)[0].start.hour == 7


def test_unknown_tzid_is_error(tmp_path):
  path = calendar(tmp_path, 'UID:x\nDTSTART;TZID=Unknown/Zone:20260921T090000\nDURATION:PT1H')
  with pytest.raises(ValueError, match='TZID'):
    load(path)


def test_event_crossing_semester_start(tmp_path):
  path = calendar(tmp_path, 'UID:x\nDTSTART:20260920T200000Z\nDTEND:20260921T100000Z')
  assert len(load(path)) == 1


def rank(events=(), **kwargs):
  options = dict(start=START, end=END, zone=LONDON, weekdays=[0], day_start=540,
                 day_end=600, duration=60, step=15)
  options.update(kwargs)
  return rank_slots(events, **options)[0]


def busy(day, start=9, end=10):
  return BusyEvent(datetime(2026, 9, day, start, tzinfo=LONDON).astimezone(UTC),
                   datetime(2026, 9, day, end, tzinfo=LONDON).astimezone(UTC), 'busy', 'uid', 'test', False)


def test_fortnightly_phases_and_denominators():
  rows = rank([busy(21)])
  by_kind = {(r['recurrence'], r['phase']): r for r in rows}
  assert by_kind['weekly', None]['free_occurrences'] == 3
  assert by_kind['fortnightly', 'A']['clashing_occurrences'] == 1
  assert by_kind['fortnightly', 'B']['free_occurrences'] == 2
  assert rows[0]['phase'] == 'B'


def test_union_overlap_and_boundary_touch():
  rows = rank([busy(21), busy(21), busy(28, 8, 9), busy(28, 10, 11)], recurrence='weekly')
  assert rows[0]['overlap_minutes'] == 60
  assert rows[0]['clashing_occurrences'] == 1
  assert len(rows[0]['clashes'][0]['events']) == 2


def test_partial_weeks_exclusions_and_anchor():
  rows = rank(start=date(2026, 9, 23), end=date(2026, 10, 6), excluded={date(2026, 9, 28)})
  assert all(row['occurrences'] == 1 for row in rows)
  assert all(row['first_date'] == '2026-10-05' for row in rows)
  assert next(row for row in rows if row['recurrence'] == 'fortnightly')['phase'] == 'A'


def test_dst_candidate_omission():
  rows, skipped = rank_slots([], date(2026, 10, 25), date(2026, 10, 25), LONDON, [6], 60, 120, 30, 30)
  assert rows == []
  assert skipped > 0


def test_dst_candidate_offset_changes_but_local_time_stays():
  row = rank(start=date(2026, 10, 19), end=date(2026, 11, 2), recurrence='weekly')[0]
  assert row['starts'] == ['2026-10-19T09:00:00+01:00', '2026-10-26T09:00:00+00:00',
                            '2026-11-02T09:00:00+00:00']


@pytest.mark.parametrize('body', [
  'UID:x\nDTSTART:20260921T090000Z\nRRULE:FREQ=NONEXISTENT',
  'UID:x\nDTSTART:20260921T090000Z\nDTEND:20260921T080000Z',
  'UID:x\nDTSTART:broken',
  'UID:x\nDTSTART:20260921T090000Z\nDURATION:-PT1H',
])
def test_malformed_events_fail(tmp_path, body):
  path = calendar(tmp_path, body)
  with pytest.raises(Exception):
    load(path)


def test_cli_json_details_and_validation(tmp_path):
  path = calendar(tmp_path, 'UID:x\nDTSTART:20260921T080000Z\nDURATION:PT1H\nSUMMARY:Lecture')
  cmd = [sys.executable, str(Path(__file__).parents[1] / 'slot_finder.py'), str(path), '--start', str(START),
         '--end', str(END), '--timezone', 'Europe/London', '--days', 'Mon', '--day-start', '09:00',
         '--day-end', '10:00']
  result = subprocess.run(cmd + ['--json'], capture_output=True, text=True)
  assert result.returncode == 0, result.stderr
  report = json.loads(result.stdout)
  assert len(report['results']) == 3
  assert report['results']['weekly'][0]['clashes'][0]['events'][0]['title'] == 'Lecture'
  text = subprocess.run(cmd + ['--details'], capture_output=True, text=True)
  assert 'Lecture' in text.stdout
  bad = subprocess.run(cmd + ['--step', '0'], capture_output=True, text=True)
  assert bad.returncode == 2
  assert 'positive' in bad.stderr


def test_this_and_future_override(tmp_path):
  path = calendar(tmp_path, '''UID:x
DTSTART;TZID=Europe/London:20260921T090000
DTEND;TZID=Europe/London:20260921T100000
RRULE:FREQ=WEEKLY;COUNT=4''', '''UID:x
RECURRENCE-ID;RANGE=THISANDFUTURE;TZID=Europe/London:20260928T090000
DTSTART;TZID=Europe/London:20260928T110000
DTEND;TZID=Europe/London:20260928T120000''')
  assert [e.start.astimezone(LONDON).hour for e in load(path)] == [9, 11, 11, 11]


def test_cancelled_override_without_dtstart(tmp_path):
  path = calendar(tmp_path, '''UID:x
DTSTART:20260921T090000Z
DURATION:PT1H
RRULE:FREQ=WEEKLY;COUNT=2''', '''UID:x
RECURRENCE-ID:20260928T090000Z
STATUS:CANCELLED''')
  assert len(load(path)) == 1


def test_zero_duration_warns(tmp_path):
  path = calendar(tmp_path, 'UID:x\nDTSTART:20260921T090000Z')
  events, warnings = load_events([path], START, END, LONDON)
  assert not events
  assert any('zero-duration' in warning for warning in warnings)


def test_multiple_calendar_files(tmp_path):
  first = calendar(tmp_path, 'UID:x\nDTSTART:20260921T090000Z\nDURATION:PT1H')
  saved = tmp_path / 'other.ics'
  first.rename(saved)
  second = calendar(tmp_path, 'UID:y\nDTSTART:20260922T090000Z\nDURATION:PT1H')
  events, _ = load_events([saved, second], START, END, LONDON)
  assert len(events) == 2


def test_floating_recurrence_exdate(tmp_path):
  path = calendar(tmp_path, '''UID:x
DTSTART:20260921T090000
DURATION:PT1H
RRULE:FREQ=WEEKLY;COUNT=3
EXDATE:20260928T090000''')
  assert [event.start.astimezone(LONDON).day for event in load(path)] == [21, 5]
