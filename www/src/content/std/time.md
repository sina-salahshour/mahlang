---
title: std:time
order: 4
section: Basics
summary: The current time, a monotonic clock for measuring, UTC dates, formatting and parsing with %-codes, ISO 8601.
---

# `std:time`

Clocks, calendar dates and text. Times are plain **Numbers of seconds**
(with milliseconds), so ordinary arithmetic adds and subtracts them; a
`time.DateTime` is the calendar reading of one, in UTC.

```mah
import time from "std:time"

let d = time.utc(1790597925)                       # seconds since 1970, UTC
print(d)                                           # 2026-09-28T12:18:45Z
print(time.format(d, "%A %d %B %Y, %H:%M"))        # Monday 28 September 2026, 12:18
print(d.weekday(), d.day_of_year())                # 1 271
```

## Two clocks

- `time.now()` is the wall-clock time, seconds since 1970-01-01 UTC. Use
  it for timestamps. It can jump when the system clock is adjusted.
- `time.monotonic()` is the seconds since the program started, and never
  goes backwards. Use it to **measure how long something takes**:

```mah
import time from "std:time"

let start = time.monotonic()
let total = 0
for let i in 0..20000 { total = total + i }
let took = time.monotonic() - start
print(total, took >= 0)                     # 199990000 true
print(time.duration_text(took) != "")      # true
```

`duration_text(seconds)` writes a duration for people: `"250ms"`, `"1.5s"`,
`"2m 5s"`, `"3h 0m 7s"`, `"2d 4h 0m 0s"`.

## Dates

Make a `DateTime` from a timestamp with `utc(seconds)`, or from its parts
with `date(year, month, day, hour = 0, minute = 0, second = 0,
millisecond = 0)`. Its fields are `year`, `month`, `day`, `hour`,
`minute`, `second` and `millisecond`.

To move through time, go through the timestamp:

```mah
import time from "std:time"

let launch = time.date(2026, 12, 31, hour: 22)
let later = time.utc(launch.timestamp() + 36 * 3600)        # 36 hours on
print(later, later.year)                                     # 2027-01-02T10:00:00Z 2027
let gap = later.timestamp() - launch.timestamp()
print(time.duration_text(gap))                               # 1d 12h 0m 0s
```

`date` checks the calendar, including leap years, and throws
`time.TimeError` for an impossible date:

```mah
import time from "std:time"

print(time.date(2028, 2, 29))                         # 2028-02-29T00:00:00Z
print(try time.date(2026, 2, 29) else "not a leap year")   # not a leap year
```

## Formatting and parsing

`format(dt, pattern)` writes a DateTime with `%`-codes, and
`parse(text, pattern)` reads one back with the same codes. Parts the
pattern leaves out are 0 (or 1 for the month and day). Month and weekday
names parse in any case.

| Code | | Code | |
|---|---|---|---|
| `%Y` | year, 4 digits | `%M` | minute, 00–59 |
| `%m` | month, 01–12 | `%S` | second, 00–59 |
| `%d` | day, 01–31 | `%f` | millisecond, 000–999 |
| `%H` | hour, 00–23 | `%j` | day of the year, 001–366 |
| `%B` `%b` | month name, full / 3 letters | `%A` `%a` | weekday name, full / 3 letters |
| `%%` | a literal `%` | | |

```mah
import time from "std:time"

let t = time.parse("03 Mar 2026 14:05", "%d %b %Y %H:%M")
print(t, time.format(t, "%Y-%m-%d (%a)"))       # 2026-03-03T14:05:00Z 2026-03-03 (Tue)

try {
    time.parse("2026-13-01", "%Y-%m-%d")
} catch {
    e: time.TimeError => { print("bad date:", e.message()) }
}
```

## ISO 8601

A DateTime prints in ISO 8601. `iso(dt)` gives the same text and
`parse_iso` reads it, with or without the time, seconds, milliseconds or
`Z`:

```mah
import time from "std:time"

let a = time.parse_iso("2026-09-28T12:01:07.250Z")
print(a.millisecond, time.iso(a))                     # 250 2026-09-28T12:01:07.250Z
print(time.parse_iso("2026-09-28"))                   # 2026-09-28T00:00:00Z
```

## Time zones

Everything is **UTC** for now; time zones aren't supported yet. Store
and compare timestamps, and convert to a calendar reading only for
display.

## Reference

| Function | |
|---|---|
| `now()` | wall-clock seconds since 1970 (UTC), with milliseconds |
| `monotonic()` | seconds since the program started, never decreasing |
| `utc(timestamp)` | the `DateTime` for a timestamp |
| `date(year, month, day, hour = 0, minute = 0, second = 0, millisecond = 0)` | a `DateTime` from parts; throws `TimeError` for an impossible one |
| `format(dt, pattern)` | text by `%`-codes; an unknown code is a RuntimeError |
| `parse(text, pattern)` | a `DateTime`; throws `TimeError` if the text doesn't match |
| `iso(dt)`, `parse_iso(text)` | ISO 8601 text and back |
| `duration_text(seconds)` | a short, human-readable duration |

| `DateTime` method | |
|---|---|
| `timestamp()` | seconds since 1970 |
| `weekday()` | Monday = 1 to Sunday = 7 |
| `day_of_year()` | from 1 |

`time.TimeError { message }` is thrown by `date`, `parse` and `parse_iso`.

Waiting is built in, not part of this module: `sleep_async(ms)` pauses
the current task and lets others run (see [Async](/docs/async)). For
timers that call you back, see [`std:async`](/std/async).
