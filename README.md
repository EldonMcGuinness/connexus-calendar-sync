# Connexus calendar sync

This script generates Connexus iCalendar feeds for Nextcloud. It reads planner events, writes feed files under the configured `ROOT` directory, and adds a 15-minute display alarm to timed events. It does not change Connexus planner events.

## Runtime setup

Python dependencies are listed in `requirements.txt`. Install Playwright's Chromium browser in the runtime used by the scheduled job. Keep credentials, student configuration, browser state, and generated feeds outside Git; `.secrets/` and `feeds/` are ignored.

Add a `CONNEXUS_STUDENTS_JSON` setting to `.secrets/calendar_sync.env` on the server. See `calendar_sync.env.example` for the format. The names and Connexus web user IDs belong in that private server-side file and are not stored in this repository.

The script currently sets its data root in `connexus_sync.py`. Keep that path aligned with the server checkout used by cron.

