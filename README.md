# Connexus calendar sync

This script generates Connexus iCalendar feeds for Nextcloud. It reads planner events, writes feed files under the configured `ROOT` directory, and adds a 15-minute display alarm to timed events. It does not change Connexus planner events.

## Runtime setup

From the server checkout directory, create the runtime and install the dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Keep credentials, student configuration, browser state, and generated feeds outside Git; `.secrets/` and `feeds/` are ignored.

Merge the needed settings from `calendar_sync.env.example` into the existing `.secrets/calendar_sync.env`; do not overwrite existing credentials. Student names and Connexus web user IDs belong only in that private server-side file. Keep the existing `CONNEXUS_UID_DOMAIN` value there so event IDs remain stable. Username and password are needed for automatic sign-in when the saved Connexus session is missing or expires. The failure-alert webhook is optional.

The data root defaults to the directory containing `connexus_sync.py`, so the server checkout directory holds its `.secrets/` and `feeds/` folders.

## Pulling updates

Run the pull from cron as the same server user that owns the checkout and its `.secrets/` directory:

```sh
git -C /path/to/connexus-calendar-sync pull --ff-only
```

Then run the sync with the checkout's virtual environment:

```sh
/path/to/connexus-calendar-sync/.venv/bin/python /path/to/connexus-calendar-sync/connexus_sync.py
```

