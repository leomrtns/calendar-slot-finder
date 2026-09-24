# Outlook semester slot finder
A local Python CLI that ranks weekly and fortnightly commitments against an ICS snapshot. It does not connect to Outlook or upload calendar data. Python 3.10+ is required; tested with Python 3.12.13. See [private export options](OUTLOOK-EXPORT.md) for the current web limitation and supported alternatives.
## Quick start
Run these commands from this tool's directory. The local environment is already installed in this workspace.
```bash
python3 -m venv .venv
TMPDIR="$PWD/work/tmp" .venv/bin/python -m pip install --no-cache-dir -r requirements.txt
.venv/bin/python slot_finder.py examples/semester.ics \
  --start 2026-09-21 --end 2026-12-11 --timezone Europe/London
```
For a fresh copy, create `work/tmp` before installation. For your own snapshot, replace the example filename:
```bash
.venv/bin/python slot_finder.py calendar.ics \
  --start 2026-09-21 --end 2026-12-11 --timezone Europe/London \
  --duration 60 --step 15 --days Mon,Tue,Wed,Thu,Fri \
  --day-start 09:00 --day-end 17:00 --top 5 --details
```
Dates here are examples, not assumed semester dates. Both boundaries are inclusive. Times and candidate weekdays use the required analysis timezone. Multiple calendars can be supplied as consecutive filenames.
## Reading the results
Each section shows the best candidates for weekly, fortnightly A, and fortnightly B commitments separately. `9/12 free (75%)` means three of the twelve proposed dates clash with at least one blocking event. It describes this snapshot; it is not a statistical prediction. A slot is fully available only when all its occurrences are free.
Candidates sort by lowest fraction of clashing dates, then lowest mean overlapping minutes per occurrence, then more occurrences, weekday and time. Nearby start times are separate alternatives. Overlapping meetings count once toward the clash-date total and their overlapping minutes are merged. Event details retain each meeting so you can identify the cause.
Week A starts on the Monday containing `--start`. Week B follows it, regardless of ISO week numbers or year boundaries. `--anchor YYYY-MM-DD` chooses a different Monday for A. The output includes the anchor and first actual date of each candidate. Only dates inside the semester contribute to denominators, including partial first/last weeks. The final date is also displayed in JSON.
## Options
| Option | Purpose |
|---|---|
| `--recurrence weekly`, `fortnightly`, or `both` | Default is `both`; fortnightly tests both phases. |
| `--duration 90 --step 15` | Meeting length and candidate grid in minutes; default 60 and 15. |
| `--days Mon,Wed,Fri` | Restrict candidate weekdays; default Monday–Friday. |
| `--day-start 08:30 --day-end 18:00` | Same-day window; `24:00` is accepted as the end. |
| `--tentative busy` or `ignore` | Tentative events block by default; ignoring them is an optimistic scenario. |
| `--all-day busy` or `ignore` | All-day events block by default; transparent/free all-day events never block. |
| `--exclude-date 2026-10-26` | Repeat to omit break/holiday dates from both commitments and denominators; does not shift fortnightly parity. |
| `--floating-timezone Europe/London` | Override the timezone for timestamps without explicit zones. |
| `--details` | Include event titles, start/end times, and tentative labels in text output. |
| `--top 5` | Five results per recurrence/phase section; default ten. |
| `--json` | Full details and proposed occurrence timestamps for selected results. |
| `--help` | Complete CLI reference. |
To inspect a particular recurring hour, narrow the day and window:
```bash
.venv/bin/python slot_finder.py calendar.ics \
  --start 2026-09-21 --end 2026-12-11 --timezone Europe/London \
  --days Tue --day-start 14:00 --day-end 15:00 --duration 60 --details
```
Save a machine-readable report inside this project:
```bash
.venv/bin/python slot_finder.py calendar.ics \
  --start 2026-09-21 --end 2026-12-11 --timezone Europe/London \
  --tentative ignore --json > work/availability.json
```
JSON includes event titles even without `--details`. Treat reports like the original calendar. No file is created unless you redirect output yourself. A malformed input or invalid option returns exit code 2; warnings about missing data are included in JSON or written to standard error in text mode.
## Calendar semantics
- Uses `icalendar` and `recurring-ical-events` to expand `RRULE`, `RDATE`, `EXDATE`, `RECURRENCE-ID`, and `RANGE=THISANDFUTURE`. Filters apply after expansion so free/cancelled/moved exceptions replace the original occurrence. Parsing/expansion errors abort instead of silently discarding broken series.
- Explicit UTC, recognized timezone identifiers, and embedded `VTIMEZONE` definitions are honored. Floating timestamps use `--floating-timezone`, otherwise `X-WR-TIMEZONE`, otherwise the analysis timezone. An unresolved explicit `TZID` is an error. An unrecognized `X-WR-TIMEZONE` requires an explicit floating timezone override.
- Candidates stay at the same local clock time across daylight-saving changes; comparison uses UTC instants. Patterns containing ambiguous/nonexistent candidate times or a clock change during the meeting are omitted with a warning. Ambiguous/nonexistent floating input times fail explicitly. Explicitly zoned event interpretations come from the calendar parser and its timezone definitions.
- `TRANSP:TRANSPARENT`, Outlook's `X-MICROSOFT-CDO-BUSYSTATUS:FREE`, and `STATUS:CANCELLED` do not block. `STATUS:TENTATIVE` and Outlook's tentative busy status follow the tentative option. Other events, including out-of-office and private events, block. Attendee response flags alone do not override calendar busy status.
- All-day `DATE` events occupy dates in the analysis timezone. `DTEND` is exclusive. Missing all-day end means one day. Timed events without an end or duration occupy zero time and generate a warning. Events crossing midnight or starting before the semester can still clash. Adjacent boundaries do not clash.
## Limits and input quality
Use a complete snapshot covering the entire semester and all relevant calendars. ICS has no reliable assertion that an exported date range is complete; the tool cannot distinguish a missing event from a free period. A zero-blocking-event warning helps catch empty or filtered exports. Data from outside the chosen range is not used to predict future events. Refresh the snapshot when your calendar changes.
Supported inputs are calendar snapshots containing `VEVENT`, not isolated meeting cancellation messages, `VFREEBUSY` reports, or PST files. RDATE values expressed as PERIOD are explicitly rejected; use a snapshot of individual occurrences for those calendars. Dense recurrence rules and very large calendars may be slow; date ranges are limited to five years. Duplicate imported calendars may repeat event details, but do not inflate overlapping minutes or clash-date counts. The tool does not infer travel time, breaks, preparation time, or unknown commitments.
## Tests
```bash
TMPDIR="$PWD/work/tmp" .venv/bin/python -m pip install --no-cache-dir -r requirements-dev.txt
TMPDIR="$PWD/work/tmp" .venv/bin/python -m pytest -q
```
Tests use synthetic calendars, not your real account. They cover exclusions/additions, moved and cancelled exceptions, THISANDFUTURE overrides, timezone conversion, embedded timezone definitions, daylight-saving transitions, all-day events, free/tentative filters, boundary overlap, ranking denominators, multiple files, malformed inputs and CLI reports. `requirements-lock.txt` records the complete tested environment.
The recurrence library's [query documentation](https://recurring-ical-events.readthedocs.io/en/latest/reference/api.html) and [examples](https://recurring-ical-events.readthedocs.io/en/latest/user-guide/examples.html) describe its expansion and error handling.
