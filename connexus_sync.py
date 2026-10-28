#!/usr/bin/env python3
"""Generate two iCalendar feeds from Connexus's authenticated Event API.

The API range is calculated every run as August 1 (inclusive) through the
following August 1 (exclusive), covering one full August–July school year.
The script only reads Connexus data. It never opens lessons or changes planner
events, and it preserves an existing feed if authentication or parsing fails.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from icalendar import Alarm, Calendar, Event
from playwright.sync_api import APIResponse, BrowserContext, Locator, Page, sync_playwright


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".secrets" / "connexus-state.json"
FEEDS_DIR = ROOT / "feeds"
TIMEZONE = ZoneInfo("America/New_York")
EVENT_API = "https://www.connexus.com/api/v1/Event"
CONNEXUS_ORIGIN = "https://www.connexus.com/"
LOGIN_URL = "https://www.connexus.com/login?sendTo=%2fdefault.aspx"

WEBHOOK_ENV = "CONNEXUS_WEBHOOK_URL"
LEGACY_WEBHOOK_ENV = "CONNEXUS_AUTH_WEBHOOK_URL"
USERNAME_ENV = "CONNEXUS_USERNAME"
PASSWORD_ENV = "CONNEXUS_PASSWORD"
STUDENTS_ENV = "CONNEXUS_STUDENTS_JSON"
UID_DOMAIN_ENV = "CONNEXUS_UID_DOMAIN"
DEFAULT_UID_DOMAIN = "connexus-calendar-sync.local"
PRIVATE_ENV_FILE = ROOT / ".secrets" / "calendar_sync.env"
FAILURE_ALERT_FILE = ROOT / ".secrets" / "last-failure-alert.txt"
AUTH_ALERT_COOLDOWN = timedelta(hours=12)


@dataclass(frozen=True)
class Student:
    name: str
    web_user_id: str


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    title: str
    starts: datetime | date
    ends: datetime | date
    all_day: bool
    event_type: str
    course: str
    description: str
    custom_lesson: bool
    live_calendar: bool
    required: bool
    source_url: str
    recurrence_rule: str
    excluded_starts: tuple[date | datetime, ...]

    def uid(self, student: Student) -> str:
        value = self.event_id
        if not value:
            value = hashlib.sha256(
                f"{student.web_user_id}|{self.title}|{self.starts}|{self.ends}".encode("utf-8")
            ).hexdigest()
        uid_domain = os.environ.get(UID_DOMAIN_ENV, DEFAULT_UID_DOMAIN).strip() or DEFAULT_UID_DOMAIN
        return f"connexus-{student.web_user_id}-{value}@{uid_domain}"


class AuthenticationRequired(RuntimeError):
    """The saved Connexus browser state no longer authorizes the Event API."""


def load_private_env() -> None:
    """Read private runtime settings without placing them in shell history."""
    try:
        lines = PRIVATE_ENV_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {WEBHOOK_ENV, LEGACY_WEBHOOK_ENV, USERNAME_ENV, PASSWORD_ENV, STUDENTS_ENV, UID_DOMAIN_ENV} and not os.environ.get(key):
            os.environ[key] = value.strip().strip("'\"")


def load_students() -> tuple[Student, ...]:
    """Load Connexus student names and web user IDs from the private env file."""
    raw_students = os.environ.get(STUDENTS_ENV, "").strip()
    if not raw_students:
        raise ValueError(f"{STUDENTS_ENV} is missing from {PRIVATE_ENV_FILE}.")

    try:
        entries = json.loads(raw_students)
    except json.JSONDecodeError as error:
        raise ValueError(f"{STUDENTS_ENV} must contain a JSON array of student objects.") from error

    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{STUDENTS_ENV} must contain a non-empty JSON array of student objects.")

    students: list[Student] = []
    seen_names: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Student entry {index} in {STUDENTS_ENV} must be an object.")
        name = entry.get("name")
        web_user_id = entry.get("web_user_id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Student entry {index} in {STUDENTS_ENV} needs a non-empty name.")
        if isinstance(web_user_id, int) and not isinstance(web_user_id, bool):
            web_user_id = str(web_user_id)
        if not isinstance(web_user_id, str) or not web_user_id.strip():
            raise ValueError(f"Student entry {index} in {STUDENTS_ENV} needs a non-empty web_user_id.")

        normalized_name = name.strip()
        name_key = normalized_name.casefold()
        if name_key in seen_names:
            raise ValueError(f"Student names in {STUDENTS_ENV} must be unique.")
        seen_names.add(name_key)
        students.append(Student(normalized_name, web_user_id.strip()))

    return tuple(students)


def notify_failure(message: str) -> None:
    """Notify IFTTT of a sync failure at most once per unresolved incident."""
    webhook_url = os.environ.get(WEBHOOK_ENV) or os.environ.get(LEGACY_WEBHOOK_ENV)
    if not webhook_url:
        logging.warning("Failure alert not sent: %s is not configured.", WEBHOOK_ENV)
        return

    now = datetime.now(timezone.utc)
    try:
        previous = datetime.fromisoformat(FAILURE_ALERT_FILE.read_text(encoding="utf-8").strip())
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        if now - previous < AUTH_ALERT_COOLDOWN:
            logging.info("Failure alert already sent within the last 12 hours.")
            return
    except FileNotFoundError:
        pass
    except ValueError:
        logging.warning("Ignoring malformed failure-alert timestamp: %s", FAILURE_ALERT_FILE)

    description = re.sub(r"\s+", " ", message).strip()[:500]
    url_parts = urlsplit(webhook_url)
    query = [(key, value) for key, value in parse_qsl(url_parts.query, keep_blank_values=True) if key not in {"value1", "value2"}]
    query.extend((("value1", "Error"), ("value2", description)))
    alert_url = urlunsplit((url_parts.scheme, url_parts.netloc, url_parts.path, urlencode(query), url_parts.fragment))
    request = Request(
        alert_url,
        headers={"User-Agent": "connexus-calendar-sync/2.0"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Webhook returned HTTP {response.status}")
    except (URLError, OSError, RuntimeError) as error:
        logging.error("Could not send failure alert: %s", error)
        return

    FAILURE_ALERT_FILE.write_text(now.isoformat(), encoding="utf-8")
    os.chmod(FAILURE_ALERT_FILE, 0o600)
    logging.info("Sent failure alert.")


def clear_failure_alert() -> None:
    try:
        FAILURE_ALERT_FILE.unlink()
    except FileNotFoundError:
        pass


def login_credentials_configured() -> bool:
    return bool(os.environ.get(USERNAME_ENV) and os.environ.get(PASSWORD_ENV))


def first_visible_locator(page: Page, selectors: tuple[str, ...], attempts: int = 1) -> Locator | None:
    """Find the first visible matching locator, allowing the Pearson form to advance.

    Pearson deliberately renders hidden username/password decoy inputs ahead of
    its actual form fields.  Do not stop at ``.first`` when a selector matches
    several elements.
    """
    for _ in range(attempts):
        for selector in selectors:
            try:
                locators = page.locator(selector)
                for index in range(locators.count()):
                    locator = locators.nth(index)
                    if locator.is_visible():
                        return locator
            except Exception:  # noqa: BLE001 - try the next selector/state
                continue
        page.wait_for_timeout(250)
    return None


def dismiss_cookie_consent(page: Page) -> None:
    """Accept Pearson's OneTrust banner when it blocks the sign-in form."""
    accept = first_visible_locator(
        page,
        ("#onetrust-consent-sdk #onetrust-accept-btn-handler",),
        attempts=2,
    )
    if accept is not None:
        accept.click(timeout=10_000)
        logging.info("Accepted Pearson cookie-consent banner.")


PASSWORD_SELECTORS = (
    "input[type='password']",
    "input[autocomplete='current-password']",
    "input[name*='password' i]",
    "input[id*='password' i]",
)


def login_page_diagnostics(page: Page) -> str:
    """Return safe field metadata when Pearson's sign-in flow changes.

    Never include form values: they could contain the account username.
    """
    try:
        location = urlsplit(page.url)
        safe_url = urlunsplit((location.scheme, location.netloc, location.path, "", ""))
        inputs = page.locator("input").evaluate_all(
            """elements => elements.map(element => ({
                type: element.getAttribute('type'),
                name: element.getAttribute('name'),
                id: element.id,
                autocomplete: element.getAttribute('autocomplete')
            }))"""
        )
        return f"Current page: {safe_url}; visible input metadata: {inputs!r}"
    except Exception:  # noqa: BLE001 - diagnostics must never hide the original error
        return "Could not inspect the current Pearson sign-in page."


def click_form_submit(page: Page) -> None:
    dismiss_cookie_consent(page)
    submit = first_visible_locator(
        page,
        (
            "#signInButton",
            "button[type='submit']",
            "input[type='submit']",
            "button[data-testid*='submit']",
        ),
        attempts=4,
    )
    if submit is None:
        for label in (r"^continue$", r"^next$", r"^sign in$", r"^log in$"):
            candidate = page.get_by_role("button", name=re.compile(label, re.IGNORECASE)).first
            try:
                if candidate.count() and candidate.is_visible():
                    submit = candidate
                    break
            except Exception:  # noqa: BLE001 - try the next label
                continue
    if submit is None:
        raise AuthenticationRequired("Could not find Pearson's sign-in/continue button.")
    submit.click(timeout=15_000)


def login_with_credentials(context: BrowserContext) -> Page:
    """Complete Connexus's normal Pearson username/password sign-in flow."""
    username = os.environ.get(USERNAME_ENV)
    password = os.environ.get(PASSWORD_ENV)
    if not username or not password:
        raise AuthenticationRequired(f"Set {USERNAME_ENV} and {PASSWORD_ENV} in {PRIVATE_ENV_FILE} to enable automatic sign-in.")

    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

    username_input = first_visible_locator(
        page,
        (
            "input[type='email']",
            "input[name='username']",
            "input[name='userName']",
            "input[name='login']",
            "input[name='landingPageData.Username']",
            "#usernameInput",
            "input[id*='username' i]",
        ),
        attempts=40,
    )
    password_input = first_visible_locator(page, PASSWORD_SELECTORS, attempts=2)

    if username_input is not None:
        username_input.fill(username, timeout=15_000)
    if password_input is None:
        if username_input is None:
            raise AuthenticationRequired("Could not find a Pearson username or password field.")
        click_form_submit(page)
        # Pearson may route the username to a different identity page before
        # rendering its password form.  Its redirect can take well over ten
        # seconds, particularly during the overnight batch window.
        password_input = first_visible_locator(page, PASSWORD_SELECTORS, attempts=180)
    if password_input is None:
        raise AuthenticationRequired(
            "Pearson did not display the password field within 45 seconds after entering the username. "
            + login_page_diagnostics(page)
        )

    password_input.fill(password, timeout=15_000)
    click_form_submit(page)
    try:
        page.wait_for_url(re.compile(r"^https://www\.connexus\.com/"), timeout=60_000)
    except Exception as error:  # noqa: BLE001 - report a concise login failure
        raise AuthenticationRequired("Pearson sign-in did not return to Connexus.") from error
    logging.info("Completed Connexus username/password sign-in.")
    return page


def save_verified_session_state(browser: Any, context: BrowserContext, student: Student, start: date) -> None:
    """Save post-login state only after a fresh context proves it can call Connexus."""
    temporary = STATE_FILE.with_name(STATE_FILE.name + ".candidate")
    probe: BrowserContext | None = None
    try:
        try:
            context.storage_state(path=str(temporary), indexed_db=True)
        except TypeError:
            context.storage_state(path=str(temporary))
        os.chmod(temporary, 0o600)

        probe = browser.new_context(storage_state=str(temporary), timezone_id=str(TIMEZONE))
        response = probe.request.get(
            api_url(student, start, start + timedelta(days=1)),
            headers={"Accept": "application/json"},
            timeout=60_000,
        )
        response_payload(response, student)
        os.replace(temporary, STATE_FILE)
        os.chmod(STATE_FILE, 0o600)
        logging.info("Saved and verified refreshed Connexus session state.")
    except Exception as error:  # noqa: BLE001 - never replace a known-good state on a failed round trip
        logging.warning("Did not replace the saved Connexus state after login: %s", error)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    finally:
        if probe is not None:
            probe.close()


def school_year_window() -> tuple[date, date]:
    """Return Aug 1 inclusive and next Aug 1 exclusive in New York time."""
    today = datetime.now(TIMEZONE).date()
    start_year = today.year if today >= date(today.year, 8, 1) else today.year - 1
    return date(start_year, 8, 1), date(start_year + 1, 8, 1)


def api_boundary(day: date) -> str:
    """Mirror Connexus's observed 04:00 local API-boundary convention."""
    return datetime.combine(day, time(4), TIMEZONE).isoformat(timespec="milliseconds")


def api_url(student: Student, start: date, end: date) -> str:
    query = urlencode(
        {
            "ForIdWebuser": student.web_user_id,
            "StartDate": api_boundary(start),
            "EndDate": api_boundary(end),
            "TimeZone": str(TIMEZONE),
        }
    )
    return f"{EVENT_API}?{query}"


def response_payload(response: APIResponse, student: Student) -> Any:
    content_type = response.headers.get("content-type", "")
    if response.status in (401, 403) or "login" in response.url.lower():
        raise AuthenticationRequired(f"{student.name}: Event API returned HTTP {response.status}.")
    if not response.ok:
        raise RuntimeError(f"{student.name}: Event API returned HTTP {response.status} ({content_type}).")
    try:
        return response.json()
    except Exception as error:  # noqa: BLE001
        body_prefix = response.text()[:250].lower()
        if "sign in" in body_prefix or "login" in body_prefix or "<html" in body_prefix:
            raise AuthenticationRequired(f"{student.name}: Event API returned a sign-in page.") from error
        raise RuntimeError(f"{student.name}: Event API did not return JSON ({content_type}).") from error


def schema(value: Any, depth: int = 0) -> Any:
    """A safe diagnostic view: field names/types only, never calendar values."""
    if depth >= 4:
        return "…"
    if isinstance(value, dict):
        return {key: schema(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [schema(value[0], depth + 1)] if value else []
    return type(value).__name__


def event_objects(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected Event API root type: {type(payload).__name__}")

    normalized = {re.sub(r"[^a-z0-9]", "", key.lower()): value for key, value in payload.items()}
    for name in ("events", "event", "data", "items", "results"):
        candidate = normalized.get(name)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    raise RuntimeError(f"Could not find an event list in API response; top-level keys: {', '.join(payload)}")


def value_for(raw: dict[str, Any], *names: str) -> Any:
    normalized = {re.sub(r"[^a-z0-9]", "", key.lower()): value for key, value in raw.items()}
    for name in names:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if value is not None:
            return value
    return None


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def plain_title(value: Any) -> str:
    """Turn Connexus's occasional HTML-formatted event title into plain text."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def calendar_course_name(value: Any) -> str:
    """Remove Connexus's internal section ID, grade prefix, and marker."""
    text = plain_title(value).rstrip("*").strip()
    text = re.sub(r"^\d+\s+", "", text)
    text = re.sub(r"^GRADE\s+\d+\s+", "", text, flags=re.IGNORECASE)
    subject_patterns = (
        (r"\bPHYSICAL\s+EDUCATION\b|\bP\.?\s*E\.?", "Physical Education"),
        (r"\bSOCIAL\s+STUDIES\b", "Social Studies"),
        (r"\bSCIENCE\b", "Science"),
        (r"\b(?:ELA|ENGLISH\s+LANGUAGE\s+ARTS)\b", "ELA"),
        (r"\bMATH(?:EMATICS)?\b", "Math"),
        (r"\bART\b", "Art"),
    )
    for pattern, subject in subject_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            term = re.search(r"\bT\d+\b|\bINTER(?:I|E)?M\s*\d+\b", text, re.IGNORECASE)
            if not term:
                return subject
            term_label = term.group(0).upper() if re.fullmatch(r"T\d+", term.group(0), re.IGNORECASE) else re.sub(
                r"\s+", " ", term.group(0).title()
            ).strip()
            return f"{subject} - {term_label}"
    if text.isupper():
        text = text.title()
    return text.replace("Ela", "ELA")


def calendar_event_title(value: Any) -> str:
    """Make a Connexus lesson label compact and readable in calendar views."""
    text = plain_title(value)
    lesson_match = re.fullmatch(r"U\s*(\d+)\s*,?\s*L\s*(\d+)\s*:\s*(.+)", text, re.IGNORECASE)
    if lesson_match:
        unit, lesson, topic = lesson_match.groups()
        return f"U{unit}L{lesson}: {topic.title()}"
    return text


def gradebook_weight_labels(value: Any) -> list[str]:
    """Return readable labels from Connexus's gradeBookWeightTypes field."""
    values = value if isinstance(value, list) else ([] if value is None else [value])
    labels: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = value_for(item, "name", "title", "label", "weightTypeName", "value")
        if item is None:
            continue
        label = plain_title(item)
        if label:
            labels.append(label)
    return labels


def parse_recurrence_datetime(value: str) -> date | datetime:
    """Parse an iCalendar DATE or DATE-TIME used by Connexus recurrence data."""
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date()
    if re.fullmatch(r"\d{8}T\d{6}Z", text):
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if re.fullmatch(r"\d{8}T\d{6}", text):
        return datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=TIMEZONE)
    raise ValueError(f"unrecognized iCalendar date value {value!r}")


def recurrence_details(value: Any) -> tuple[str, tuple[date | datetime, ...]]:
    """Extract Connexus's RRULE plus any EXDATE recurrence exceptions."""
    recurrence = re.sub(r"\r?\n[ \t]", "", str(value or "")).strip()
    embedded_rule = re.search(r"(?im)^RRULE:(.+)$", recurrence)
    if embedded_rule:
        rule = embedded_rule.group(1).strip()
    else:
        rule = recurrence
    if rule.upper().startswith("RRULE:"):
        rule = rule.split(":", 1)[1].strip()
    if not rule:
        return "", ()
    rule = ";".join(
        part for part in rule.split(";") if "=" in part and part.split("=", 1)[1].strip()
    )
    if not re.search(r"(?:^|;)FREQ=", rule, re.IGNORECASE):
        logging.warning("Ignoring unrecognized Connexus recurrence rule: %s", rule[:100])
        return "", ()

    excluded: list[date | datetime] = []
    for match in re.finditer(r"(?im)^EXDATE(?:;[^:]+)?:([^\r\n]+)$", recurrence):
        for raw_date in match.group(1).split(","):
            try:
                excluded.append(parse_recurrence_datetime(raw_date))
            except ValueError:
                logging.warning("Ignoring unrecognized Connexus EXDATE: %s", raw_date[:100])
    return rule, tuple(excluded)


def parse_api_date(value: Any) -> datetime | date:
    if isinstance(value, datetime):
        return value.astimezone(TIMEZONE) if value.tzinfo else value.replace(tzinfo=TIMEZONE)
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Expected a date string, received {type(value).__name__}")

    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(TIMEZONE) if parsed.tzinfo else parsed.replace(tzinfo=TIMEZONE)
    except ValueError as error:
        raise ValueError(f"Unrecognized Event API date: {value!r}") from error


def normalize_event(raw: dict[str, Any], student: Student) -> CalendarEvent:
    title = value_for(raw, "title", "eventTitle", "eventName", "name", "subject")
    starts_raw = value_for(raw, "start", "startDate", "startDateTime", "startTime", "date")
    ends_raw = value_for(raw, "end", "endDate", "endDateTime", "endTime")
    if not title or starts_raw is None:
        raise ValueError("missing title or start field")

    starts = parse_api_date(starts_raw)
    ends = parse_api_date(ends_raw) if ends_raw is not None else starts
    all_day = as_bool(value_for(raw, "allDay", "isAllDay", "allDayEvent")) or isinstance(starts, date) and not isinstance(starts, datetime)

    if all_day:
        start_day = starts.date() if isinstance(starts, datetime) else starts
        end_day = ends.date() if isinstance(ends, datetime) else ends
        if end_day <= start_day:
            end_day = start_day + timedelta(days=1)
        starts, ends = start_day, end_day
    else:
        assert isinstance(starts, datetime)
        assert isinstance(ends, datetime)
        if ends <= starts:
            ends = starts + timedelta(minutes=30)

    section_name = value_for(raw, "sectionName", "course", "courseName", "courseTitle", "className")
    course = calendar_course_name(section_name)
    base_type = str(value_for(raw, "baseType", "eventType", "type", "category") or "").strip()
    event_type_id = str(value_for(raw, "idEventType") or "").strip()
    is_mandatory = as_bool(value_for(raw, "isMandatory", "required", "isRequired", "attendanceRequired"))
    # Connexus has more than one idEventType for instructional items.  The
    # display/grade-book behavior is determined by the broad base type.
    is_lesson = base_type.casefold() == "lesson"
    is_custom_lesson = base_type.casefold() == "custom lesson"
    source_url = str(value_for(raw, "url", "eventUrl", "lessonUrl", "link") or "").strip()
    if source_url:
        source_url = urljoin(CONNEXUS_ORIGIN, source_url)
    event_description = html.unescape(str(value_for(raw, "description", "details", "notes") or "").strip())
    clean_title = calendar_event_title(title) if is_lesson else plain_title(title)
    display_title = f"{course} - {clean_title}" if is_lesson and course else clean_title
    content_marks_live_lesson = bool(
        re.search(r"live\s*lesson", f"{plain_title(title)} {plain_title(event_description)}", re.IGNORECASE)
    )
    is_live_lesson = (
        content_marks_live_lesson
        or re.search(r"live\s+lessons?", plain_title(section_name), re.IGNORECASE)
        or re.search(r"live[-_]?lesson|zoom", source_url, re.IGNORECASE)
    )
    # Custom Lesson items have always been the Live feed.  Connexus sometimes
    # publishes a LiveLesson as a regular Lesson, sometimes identifying it
    # only in the description, so those events belong there as well.
    live_calendar = is_custom_lesson or content_marks_live_lesson
    live_prefix = ""
    if is_live_lesson:
        lesson_text = f"{plain_title(title)} {plain_title(event_description)}"
        if is_mandatory or re.search(r"\brequired\b", lesson_text, re.IGNORECASE):
            live_prefix = "🌐🚨 "
        elif re.search(r"\boptional\b", lesson_text, re.IGNORECASE):
            live_prefix = "🌐👍 "
        else:
            live_prefix = "🌐 "
    lesson = value_for(raw, "lesson")
    if isinstance(lesson, dict) and as_bool(value_for(lesson, "completionNeedsApproval")):
        completion_prefix = "🟡 "
    elif isinstance(lesson, dict) and value_for(lesson, "completed") is not None:
        completion_prefix = "🟢 "
    else:
        completion_prefix = ""
    display_title = completion_prefix + live_prefix + display_title
    if is_custom_lesson and source_url:
        event_description = "\n".join(part for part in (event_description, source_url) if part)
    elif is_lesson and isinstance(lesson, dict):
        weights = gradebook_weight_labels(value_for(lesson, "gradeBookWeightTypes"))
        if weights:
            event_description = "\n".join(part for part in (event_description, " ".join(f"[{weight}]" for weight in weights)) if part)

    rule, excluded_starts = recurrence_details(value_for(raw, "recurrenceRule", "rrule"))

    return CalendarEvent(
        event_id=str(value_for(raw, "id", "eventId", "calendarEventId", "plannerItemId", "lessonId") or ""),
        title=display_title,
        starts=starts,
        ends=ends,
        all_day=all_day,
        event_type=base_type or event_type_id or "Planner Event",
        course=course,
        description=event_description,
        custom_lesson=is_custom_lesson,
        live_calendar=live_calendar,
        required=is_mandatory,
        source_url=source_url,
        recurrence_rule=rule,
        excluded_starts=excluded_starts,
    )


def fetch_events(context: BrowserContext, student: Student, start: date, end: date) -> tuple[list[CalendarEvent], Any]:
    url = api_url(student, start, end)
    logging.info("Fetching %s Event API range %s through %s", student.name, start, end)
    response = context.request.get(url, headers={"Accept": "application/json"}, timeout=60_000)
    payload = response_payload(response, student)
    raw_events = event_objects(payload)

    events: dict[str, CalendarEvent] = {}
    rejected = 0
    for raw in raw_events:
        try:
            item = normalize_event(raw, student)
        except ValueError as error:
            rejected += 1
            logging.warning("%s: skipped unrecognized event object (%s); keys: %s", student.name, error, ", ".join(raw))
            continue
        events[item.uid(student)] = item

    if raw_events and not events:
        raise RuntimeError(f"{student.name}: Event API returned {len(raw_events)} objects but none matched the known field names.")
    if rejected:
        logging.warning("%s: skipped %s unrecognized API events.", student.name, rejected)
    # Connexus may return both all-day events (``date``) and scheduled events
    # (``datetime``). Python intentionally does not order those types directly.
    # Normalize only the sort key; preserve their original types for iCalendar.
    def sort_key(item: CalendarEvent) -> tuple[date, time, str]:
        if isinstance(item.starts, datetime):
            return item.starts.date(), item.starts.timetz().replace(tzinfo=None), item.title.casefold()
        return item.starts, time.min, item.title.casefold()

    return sorted(events.values(), key=sort_key), payload


def calendar_for(student: Student, calendar_name: str, items: list[CalendarEvent]) -> Calendar:
    calendar = Calendar()
    calendar.add("prodid", "-//Connexus Calendar Sync//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-calname", f"{student.name} {calendar_name}")
    calendar.add("x-wr-timezone", str(TIMEZONE))

    generated_at = datetime.now(timezone.utc)
    for item in items:
        event = Event()
        event.add("uid", item.uid(student))
        event.add("dtstamp", generated_at)
        event.add("last-modified", generated_at)
        event.add("summary", item.title)
        event.add("categories", item.event_type)
        event.add("class", "PRIVATE")
        if item.source_url:
            event.add("url", item.source_url)
        if item.recurrence_rule:
            event.add("rrule", item.recurrence_rule)
        for excluded_start in item.excluded_starts:
            event.add("exdate", excluded_start)

        if item.custom_lesson:
            description = [item.description] if item.description else []
        else:
            description = [f"Connexus event type: {item.event_type}"]
            if item.course:
                description.append(f"Course: {item.course}")
            if item.description:
                description.append(item.description)
            if item.required:
                description.append("Attendance: required")
        if description:
            event.add("description", "\n".join(description))
        event.add("dtstart", item.starts)
        event.add("dtend", item.ends)
        if not item.all_day:
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", item.title)
            alarm.add("trigger", -timedelta(minutes=15))
            event.add_component(alarm)
        calendar.add_component(event)
    return calendar


def write_calendar(path: Path, calendar: Calendar) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(calendar.to_ical())
    os.replace(temporary, path)
    os.chmod(path, 0o644)


def parse_args(students: tuple[Student, ...]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and parse API data but do not write feeds.")
    parser.add_argument("--dump-schema", action="store_true", help="Print response field names/types only; do not write feeds.")
    parser.add_argument("--student", choices=[student.name.lower() for student in students], action="append")
    return parser.parse_args()


def main() -> int:
    load_private_env()
    try:
        students = load_students()
    except ValueError as error:
        message = f"Connexus calendar sync configuration error: {error}"
        logging.error(message)
        notify_failure(message)
        return 2

    args = parse_args(students)
    if not STATE_FILE.is_file() and not login_credentials_configured():
        message = f"Connexus calendar sync needs attention: {STATE_FILE.name} is missing and automatic sign-in credentials are not configured."
        logging.error(message)
        notify_failure(message)
        return 2

    start, end = school_year_window()
    selected = [student for student in students if not args.student or student.name.lower() in args.student]
    logging.info("School-year API range: %s through %s (exclusive)", start, end)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context: BrowserContext | None = None
                try:
                    logged_in_with_password = False
                    if STATE_FILE.is_file():
                        context = browser.new_context(storage_state=str(STATE_FILE), timezone_id=str(TIMEZONE))
                    else:
                        context = browser.new_context(timezone_id=str(TIMEZONE))
                        login_with_credentials(context)
                        logged_in_with_password = True

                    try:
                        results = [(student, *fetch_events(context, student, start, end)) for student in selected]
                    except AuthenticationRequired:
                        if not login_credentials_configured():
                            raise
                        logging.info("Saved session was rejected; retrying with username/password sign-in.")
                        context.close()
                        context = browser.new_context(timezone_id=str(TIMEZONE))
                        login_with_credentials(context)
                        logged_in_with_password = True
                        results = [(student, *fetch_events(context, student, start, end)) for student in selected]

                    if logged_in_with_password:
                        save_verified_session_state(browser, context, selected[0], start)

                    for student, items, payload in results:
                        if args.dump_schema:
                            print(f"{student.name} API schema:")
                            print(json.dumps(schema(payload), indent=2))
                        else:
                            logging.info("%s: %s normalized calendar events", student.name, len(items))
                            classes = [item for item in items if not item.live_calendar]
                            live = [item for item in items if item.live_calendar]
                            logging.info(
                                "%s: %s class/system events and %s Live calendar events",
                                student.name,
                                len(classes),
                                len(live),
                            )
                            if not args.dry_run:
                                class_output = FEEDS_DIR / f"{student.name.lower()}-classes.ics"
                                live_output = FEEDS_DIR / f"{student.name.lower()}-live.ics"
                                write_calendar(class_output, calendar_for(student, "Classes", classes))
                                write_calendar(live_output, calendar_for(student, "Live", live))
                                logging.info("Wrote %s and %s", class_output, live_output)
                finally:
                    if context is not None:
                        context.close()
            finally:
                browser.close()
    except AuthenticationRequired as error:
        message = "Connexus calendar sync needs attention: the saved session was rejected and automatic username/password sign-in did not succeed."
        logging.error("%s (%s)", message, error)
        notify_failure(message)
        return 3
    except Exception as error:  # noqa: BLE001 - never replace a valid feed after a failure
        logging.exception("Sync failed; existing feeds were left untouched: %s", error)
        notify_failure(f"Sync failed: {type(error).__name__}: {error}")
        return 1

    clear_failure_alert()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())

