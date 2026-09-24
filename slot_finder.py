#!/usr/bin/env python3
"""Rank recurring commitments against local iCalendar snapshots."""
import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from dateutil.tz import datetime_ambiguous, datetime_exists
from icalendar import Calendar
import recurring_ical_events

UTC = timezone.utc
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def localize(value, zone):
  """Never silently guess a floating datetime during a clock transition."""
  result = value.replace(tzinfo=zone)
  if not datetime_exists(result) or datetime_ambiguous(result):
    raise ValueError(f'Ambiguous or nonexistent local time: {value} in {zone}')
  return result


def instant(value, zone):
  if isinstance(value, datetime):
    return value if value.tzinfo else localize(value, zone)
  return localize(datetime.combine(value, time()), zone)


@dataclass(frozen=True)
class BusyEvent:
  start: datetime
  end: datetime
  title: str
  uid: str
  source: str
  tentative: bool


def load_events(paths, start, end, zone, floating_zone=None, tentative='busy', all_day='busy'):
  """Expand first, then filter: free/moved/cancelled exceptions must replace their masters."""
  lower = instant(start, zone).astimezone(UTC)
  upper = instant(end + timedelta(days=1), zone).astimezone(UTC)
  events = []
  warnings = []
  for path in paths:
    calendar = Calendar.from_ical(Path(path).read_bytes())
    if calendar.name != 'VCALENDAR':
      raise ValueError(f'{path}: expected VCALENDAR')
    fallback = floating_zone or calendar.get('X-WR-TIMEZONE') or zone
    if isinstance(fallback, str):
      try:
        fallback = ZoneInfo(str(fallback))
      except (KeyError, ValueError) as exc:
        raise ValueError(f'Unrecognized calendar timezone {fallback!r}; use --floating-timezone') from exc
    # Interpret floating times before expansion, including recurrence exclusions and overrides.
    # Remove X-WR-TIMEZONE so the dependency cannot reinterpret our explicit timezone choice.
    calendar.pop('X-WR-TIMEZONE', None)
    for event in calendar.walk('VEVENT'):
      if event.errors:
        raise ValueError(f'{path}: malformed event: {event.errors}')
      if 'UID' not in event:
        raise ValueError(f'{path}: event missing UID')
      if 'DTSTART' not in event:
        if str(event.get('STATUS', '')).upper() == 'CANCELLED' and 'RECURRENCE-ID' in event:
          event.add('DTSTART', event.decoded('RECURRENCE-ID'))
        else:
          raise ValueError(f'{path}: event missing DTSTART')
      if 'DTEND' in event and 'DURATION' in event:
        raise ValueError(f'{path}: event has both DTEND and DURATION')
      for key in ('DTSTART', 'DTEND', 'RECURRENCE-ID', 'RDATE', 'EXDATE'):
        properties = event.get(key, [])
        if not isinstance(properties, list):
          properties = [properties]
        for prop in properties:
          values = prop.dts if hasattr(prop, 'dts') else [prop]
          for item in values:
            value = item.dt
            if isinstance(value, tuple):
              raise ValueError('RDATE PERIOD values are unsupported; export individual occurrences instead')
            if isinstance(value, datetime) and value.tzinfo is None:
              if prop.params.get('TZID') or getattr(item, 'params', {}).get('TZID'):
                raise ValueError(f'{path}: unresolved TZID; include its VTIMEZONE definition')
              item.dt = localize(value, fallback)
      first = event.decoded('DTSTART')
      last = event.decoded('DTEND', None)
      if last is not None:
        if isinstance(first, datetime) != isinstance(last, datetime):
          raise ValueError(f'{path}: DTSTART and DTEND must have matching value types')
        if instant(last, fallback).astimezone(UTC) < instant(first, fallback).astimezone(UTC):
          raise ValueError(f'{path}: event ends before it starts')
      if event.decoded('DURATION', timedelta()) < timedelta():
        raise ValueError(f'{path}: negative event duration')
    expanded = recurring_ical_events.of(calendar, skip_bad_series=False).between(lower, upper)
    for event in expanded:
      status = str(event.get('STATUS', '')).upper()
      ms_busy = str(event.get('X-MICROSOFT-CDO-BUSYSTATUS', '')).upper()
      is_tentative = status == 'TENTATIVE' or ms_busy == 'TENTATIVE'
      if status == 'CANCELLED' or str(event.get('TRANSP', '')).upper() == 'TRANSPARENT' or ms_busy == 'FREE':
        continue
      if is_tentative and tentative == 'ignore':
        continue
      first = event.decoded('DTSTART')
      is_all_day = not isinstance(first, datetime)
      if is_all_day and all_day == 'ignore':
        continue
      last = event.decoded('DTEND', None)
      if last is None:
        last = first + event.decoded('DURATION', timedelta(days=1) if is_all_day else timedelta())
      # DATE events occupy local dates in the analysis timezone; DTEND is exclusive.
      begin = instant(first, zone).astimezone(UTC)
      finish = instant(last, zone).astimezone(UTC)
      if finish < begin:
        raise ValueError(f'{path}: expanded event ends before it starts')
      if finish == begin:
        warnings.append(f'{Path(path).name}: zero-duration event does not block time')
      if begin < upper and finish > lower and finish > begin:
        events.append(BusyEvent(begin, finish, str(event.get('SUMMARY', '(untitled)')),
                                str(event.get('UID', '')), str(path), is_tentative))
  if not events:
    warnings.append('No blocking occurrences found. Check export coverage and filtering before trusting free slots.')
  return sorted(events, key=lambda event: event.start), sorted(set(warnings))


def union_minutes(intervals):
  total = 0.0
  previous_start = previous_end = None
  for start, end in sorted(intervals):
    if previous_end is None or start > previous_end:
      if previous_end is not None:
        total += (previous_end - previous_start).total_seconds() / 60
      previous_start, previous_end = start, end
    else:
      previous_end = max(previous_end, end)
  if previous_end is not None:
    total += (previous_end - previous_start).total_seconds() / 60
  return total


def rank_slots(events, start, end, zone, weekdays, day_start, day_end, duration, step,
               recurrence='both', anchor=None, excluded=()):
  anchor = anchor or start - timedelta(days=start.weekday())
  indexed = defaultdict(list)
  for event in events:
    first = max(start, event.start.astimezone(zone).date())
    last = min(end, (event.end - timedelta(microseconds=1)).astimezone(zone).date())
    while first <= last:
      indexed[first].append(event)
      first += timedelta(days=1)
  modes = [('weekly', None, 1)] if recurrence == 'weekly' else []
  if recurrence == 'both':
    modes.append(('weekly', None, 1))
  if recurrence in ('fortnightly', 'both'):
    modes.extend([('fortnightly', 'A', 2), ('fortnightly', 'B', 2)])
  candidates, skipped = [], 0
  for kind, phase, interval in modes:
    for weekday in weekdays:
      dates = []
      current = start + timedelta(days=(weekday - start.weekday()) % 7)
      while current <= end:
        parity = ((current - anchor).days // 7) % 2
        if current not in excluded and (interval == 1 or parity == (phase == 'B')):
          dates.append(current)
        current += timedelta(days=7)
      if not dates:
        continue
      for minute in range(day_start, day_end - duration + 1, step):
        clashes, occurrences, overlaps = [], [], 0.0
        try:
          for day in dates:
            wall = datetime.combine(day, time()) + timedelta(minutes=minute)
            begin = localize(wall, zone)
            finish = localize(wall + timedelta(minutes=duration), zone)
            # A requested duration is a wall-clock duration; omit patterns crossing a DST transition.
            if (finish.astimezone(UTC) - begin.astimezone(UTC)) != timedelta(minutes=duration):
              raise ValueError('Clock change during candidate')
            begin_utc, finish_utc = begin.astimezone(UTC), finish.astimezone(UTC)
            occurrences.append(begin.isoformat())
            conflicts = [event for event in indexed[day] if event.start < finish_utc and event.end > begin_utc]
            if conflicts:
              minutes = union_minutes([(max(begin_utc, event.start), min(finish_utc, event.end))
                                       for event in conflicts])
              overlaps += minutes
              clashes.append({'date': day.isoformat(), 'overlap_minutes': minutes, 'events': [
                {'title': event.title, 'uid': event.uid, 'source': event.source,
                 'start': event.start.astimezone(zone).isoformat(), 'end': event.end.astimezone(zone).isoformat(),
                 'tentative': event.tentative} for event in conflicts]})
        except ValueError:
          skipped += 1
          continue
        candidates.append({'recurrence': kind, 'phase': phase, 'weekday': DAYS[weekday],
                           'time': f'{minute // 60:02}:{minute % 60:02}', 'duration_minutes': duration,
                           'occurrences': len(dates), 'free_occurrences': len(dates) - len(clashes),
                           'clashing_occurrences': len(clashes), 'free_percent': 100 * (1 - len(clashes) / len(dates)),
                           'overlap_minutes': overlaps, 'first_date': dates[0].isoformat(),
                           'last_date': dates[-1].isoformat(), 'starts': occurrences, 'clashes': clashes})
  candidates.sort(key=lambda row: (Fraction(row['clashing_occurrences'], row['occurrences']),
                                   row['overlap_minutes'] / row['occurrences'], -row['occurrences'],
                                   DAYS.index(row['weekday']), row['time'], row['phase'] or ''))
  return candidates, skipped


def clock_minutes(value):
  try:
    hour, minute = map(int, value.split(':'))
    if not (0 <= hour <= 24 and 0 <= minute < 60 and (hour < 24 or minute == 0)):
      raise ValueError()
    return hour * 60 + minute
  except ValueError as exc:
    raise argparse.ArgumentTypeError('Expected HH:MM between 00:00 and 24:00') from exc


def make_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('calendars', nargs='+', type=Path, help='One or more local .ics files')
  parser.add_argument('--start', type=date.fromisoformat, required=True, help='First semester date, inclusive')
  parser.add_argument('--end', type=date.fromisoformat, required=True, help='Last semester date, inclusive')
  parser.add_argument('--timezone', required=True, help='IANA analysis timezone, e.g. Europe/London')
  parser.add_argument('--floating-timezone', help='Override X-WR-TIMEZONE for times without timezone')
  parser.add_argument('--days', default='Mon,Tue,Wed,Thu,Fri', help='Comma-separated Mon,...,Sun')
  parser.add_argument('--day-start', type=clock_minutes, default=540)
  parser.add_argument('--day-end', type=clock_minutes, default=1020)
  parser.add_argument('--duration', type=int, default=60, help='Commitment length in minutes (default: 60)')
  parser.add_argument('--step', type=int, default=15, help='Candidate start grid in minutes (default: 15)')
  parser.add_argument('--recurrence', choices=('weekly', 'fortnightly', 'both'), default='both')
  parser.add_argument('--anchor', type=date.fromisoformat, help='Monday of fortnightly week A (default: start week)')
  parser.add_argument('--exclude-date', action='append', type=date.fromisoformat, default=[],
                      help='Omit a date from commitments and denominator; repeat for breaks')
  parser.add_argument('--tentative', choices=('busy', 'ignore'), default='busy')
  parser.add_argument('--all-day', choices=('busy', 'ignore'), default='busy')
  parser.add_argument('--top', type=int, default=10, help='Results per recurrence/phase (default: 10)')
  parser.add_argument('--details', action='store_true', help='Show clashing event titles, times and dates')
  parser.add_argument('--json', action='store_true', help='Machine-readable report including full clash details')
  return parser


def main(argv=None):
  parser = make_parser()
  args = parser.parse_args(argv)
  try:
    zone = ZoneInfo(args.timezone)
    floating = ZoneInfo(args.floating_timezone) if args.floating_timezone else None
    weekdays = sorted({DAYS.index(day.strip().title()) for day in args.days.split(',')})
    if args.start > args.end:
      raise ValueError('--start must be on or before --end')
    if args.end - args.start > timedelta(days=366 * 5):
      raise ValueError('Use a semester range of at most five years')
    if args.duration <= 0 or args.step <= 0 or args.top <= 0:
      raise ValueError('--duration, --step and --top must be positive')
    if not 0 <= args.day_start < args.day_end <= 1440 or args.duration > args.day_end - args.day_start:
      raise ValueError('Duration must fit inside a same-day working window')
    if args.anchor and args.anchor.weekday() != 0:
      raise ValueError('--anchor must be a Monday')
    anchor = args.anchor or args.start - timedelta(days=args.start.weekday())
    events, warnings = load_events(args.calendars, args.start, args.end, zone, floating,
                                   args.tentative, args.all_day)
    rows, skipped = rank_slots(events, args.start, args.end, zone, weekdays, args.day_start, args.day_end,
                               args.duration, args.step, args.recurrence, anchor, set(args.exclude_date))
    if skipped:
      warnings.append(f'Omitted {skipped} patterns with ambiguous/nonexistent times or clock changes.')
    if not rows:
      warnings.append('No eligible recurring slots in this date range and working window.')
    groups = defaultdict(list)
    for row in rows:
      key = row['recurrence'] + (f" {row['phase']}" if row['phase'] else '')
      if len(groups[key]) < args.top:
        groups[key].append(row)
    report = {'semester_start': str(args.start), 'semester_end': str(args.end), 'timezone': args.timezone,
              'week_a_monday': str(anchor), 'blocking_events': len(events), 'tentative': args.tentative,
              'all_day': args.all_day, 'excluded_dates': [str(day) for day in args.exclude_date],
              'warnings': warnings, 'results': dict(groups)}
    if args.json:
      print(json.dumps(report, indent=2))
    else:
      print(f'{args.start} to {args.end} inclusive | {zone} | week A begins {anchor}')
      print('Free % is the share of proposed dates without a clash, not a prediction of future changes.')
      print(f'{len(events)} blocking occurrences; tentative={args.tentative}; all-day={args.all_day}')
      for warning in warnings:
        print(f'Warning: {warning}', file=sys.stderr)
      for group, results in groups.items():
        print(f'\n{group.upper()}')
        for index, row in enumerate(results, 1):
          print(f"{index:2}. {row['weekday']} {row['time']} ({row['duration_minutes']} min): "
                f"{row['free_occurrences']}/{row['occurrences']} free ({row['free_percent']:.1f}%); "
                f"first {row['first_date']}; {row['overlap_minutes']:g} overlap minutes")
          if row['clashes']:
            print('    Clash dates: ' + ', '.join(clash['date'] for clash in row['clashes']))
          if args.details:
            for clash in row['clashes']:
              for event in clash['events']:
                title = ' '.join(event['title'].split())
                print(f"    {event['start']} – {event['end']}: {title}"
                      + (' [tentative]' if event['tentative'] else ''))
    return 0
  except Exception as exc:
    # Calendar dependencies use several exception types. Never report availability after a parse failure.
    parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
  sys.exit(main())
