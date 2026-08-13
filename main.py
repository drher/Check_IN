import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import requests


CONFIG_PATH = Path("config.json")
LOG_DIR = Path("logs")
RUNTIME_DIR = Path("runtime")
CAPTCHA_DIR = RUNTIME_DIR / "captcha"


@dataclass
class SelectorConfig:
    username: str
    password: str
    captcha_input: str
    captcha_image: str | None = None
    login_submit: str
    target_button: str
    post_login_ready: str | None = None
    target_done: str | None = None


@dataclass
class SiteConfig:
    login_url: str
    username: str
    password: str
    browser: str
    headless: bool


@dataclass
class ScheduleConfig:
    times: list[str]
    days: list[str]
    timezone: str


@dataclass
class TimingConfig:
    navigation_timeout_ms: int
    wait_timeout_ms: int


@dataclass
class RetryConfig:
    attempts: int
    wait_seconds: int


@dataclass
class CaptchaConfig:
    input_mode: str
    wait_seconds: int
    poll_seconds: int


@dataclass
class LineConfig:
    enabled: bool
    channel_access_token: str
    user_id: str
    public_base_url: str
    inbox_file: str


@dataclass
class AppConfig:
    schedule: ScheduleConfig
    site: SiteConfig
    selectors: SelectorConfig
    timing: TimingConfig
    retry: RetryConfig
    captcha: CaptchaConfig
    line: LineConfig


DAY_MAP = {
    "mon": "mon",
    "tue": "tue",
    "wed": "wed",
    "thu": "thu",
    "fri": "fri",
    "sat": "sat",
    "sun": "sun",
}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(
            "config.json not found. Copy config.example.json to config.json and fill your values."
        )

    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    schedule = ScheduleConfig(**raw["schedule"])
    site = SiteConfig(**raw["site"])
    selectors = SelectorConfig(**raw["selectors"])
    timing = TimingConfig(**raw["timing"])
    retry = RetryConfig(**raw["retry"])
    captcha = CaptchaConfig(**raw.get("captcha", {}))
    line = LineConfig(**raw.get("line", {}))

    return AppConfig(
        schedule=schedule,
        site=site,
        selectors=selectors,
        timing=timing,
        retry=retry,
        captcha=captcha,
        line=line,
    )


def load_line_replies(inbox_path: Path) -> list[dict[str, Any]]:
    if not inbox_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    for raw_line in inbox_path.read_text(encoding="utf-8").splitlines():
        text = raw_line.strip()
        if not text:
            continue
        try:
            entries.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return entries


def build_captcha_image(page, config: AppConfig) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    file_path = CAPTCHA_DIR / f"captcha-{stamp}.png"

    if config.selectors.captcha_image:
        page.locator(config.selectors.captcha_image).first.screenshot(path=str(file_path))
    else:
        page.screenshot(path=str(file_path), full_page=True)

    latest_path = CAPTCHA_DIR / "current.png"
    latest_path.write_bytes(file_path.read_bytes())
    logging.info("Saved captcha image: %s", latest_path)
    return latest_path


def send_line_captcha(config: AppConfig, image_url: str) -> None:
    endpoint = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {config.line.channel_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": config.line.user_id,
        "messages": [
            {
                "type": "text",
                "text": "請回覆此驗證碼訊息的數字，我會自動填入網頁。",
            },
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            },
        ],
    }

    response = requests.post(endpoint, headers=headers, json=payload, timeout=15)
    if response.status_code >= 300:
        raise RuntimeError(f"LINE push failed: {response.status_code} {response.text}")


def wait_for_line_captcha(config: AppConfig, request_time: datetime) -> str:
    import time

    inbox_path = Path(config.line.inbox_file)
    deadline = time.time() + config.captcha.wait_seconds

    while time.time() < deadline:
        entries = load_line_replies(inbox_path)
        for item in reversed(entries):
            created_at = item.get("created_at", "")
            text = str(item.get("text", "")).strip()
            if not text:
                continue

            try:
                created_dt = datetime.fromisoformat(created_at)
            except ValueError:
                continue

            if created_dt >= request_time:
                normalized = "".join(re.findall(r"\d+", text)) or text
                return normalized

        time.sleep(max(config.captcha.poll_seconds, 1))

    raise TimeoutError("Timed out waiting for LINE captcha reply")


def get_captcha_text(page, config: AppConfig) -> str:
    mode = config.captcha.input_mode.lower().strip()

    if mode == "manual":
        logging.info("Captcha detected. Enter the captcha shown in the browser.")
        captcha_text = input("Enter captcha: ").strip()
        if not captcha_text:
            raise ValueError("Captcha cannot be empty.")
        return captcha_text

    if mode == "line":
        if not config.line.enabled:
            raise ValueError("line.enabled must be true when captcha.input_mode is line")
        if not config.line.channel_access_token or not config.line.user_id or not config.line.public_base_url:
            raise ValueError("LINE settings are incomplete")

        build_captcha_image(page, config)
        now = datetime.now()
        image_url = f"{config.line.public_base_url.rstrip('/')}/captcha/current.png?t={int(now.timestamp())}"
        send_line_captcha(config, image_url)
        logging.info("Sent captcha image to LINE, waiting for reply")
        return wait_for_line_captcha(config, now)

    raise ValueError(f"Unsupported captcha input mode: {config.captcha.input_mode}")


def take_error_screenshot(page, prefix: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    file_path = LOG_DIR / f"{prefix}-{stamp}.png"
    page.screenshot(path=str(file_path), full_page=True)
    logging.info("Saved screenshot: %s", file_path)


def run_once(config: AppConfig) -> None:
    logging.info("Job started")

    with sync_playwright() as p:
        browser_type = getattr(p, config.site.browser, None)
        if browser_type is None:
            raise ValueError(f"Unsupported browser type: {config.site.browser}")

        browser = browser_type.launch(headless=config.site.headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(
                config.site.login_url,
                wait_until="domcontentloaded",
                timeout=config.timing.navigation_timeout_ms,
            )

            page.fill(config.selectors.username, config.site.username)
            page.fill(config.selectors.password, config.site.password)

            if config.selectors.captcha_input:
                captcha_text = get_captcha_text(page, config)
                page.fill(config.selectors.captcha_input, captcha_text)

            page.click(config.selectors.login_submit)

            if config.selectors.post_login_ready:
                page.wait_for_selector(
                    config.selectors.post_login_ready,
                    timeout=config.timing.wait_timeout_ms,
                )

            page.click(config.selectors.target_button)

            if config.selectors.target_done:
                page.wait_for_selector(
                    config.selectors.target_done,
                    timeout=config.timing.wait_timeout_ms,
                )

            logging.info("Target button clicked successfully")

        except Exception:
            take_error_screenshot(page, "error")
            raise
        finally:
            context.close()
            browser.close()


def run_with_retry(config: AppConfig) -> None:
    last_error: Exception | None = None

    for attempt in range(1, config.retry.attempts + 1):
        try:
            logging.info("Attempt %s/%s", attempt, config.retry.attempts)
            run_once(config)
            return
        except (PlaywrightTimeoutError, ValueError, RuntimeError) as error:
            last_error = error
            logging.error("Attempt failed: %s", error)
            if attempt < config.retry.attempts:
                import time

                time.sleep(config.retry.wait_seconds)

    if last_error:
        raise last_error


def validate_schedule(config: ScheduleConfig) -> None:
    if not config.times:
        raise ValueError("schedule.times cannot be empty")
    if not config.days:
        raise ValueError("schedule.days cannot be empty")

    for day in config.days:
        if day not in DAY_MAP:
            raise ValueError(f"Unsupported day value: {day}")

    for value in config.times:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid time format: {value}. Use HH:MM")
        hour, minute = parts
        if not hour.isdigit() or not minute.isdigit():
            raise ValueError(f"Invalid time format: {value}. Use HH:MM")
        h, m = int(hour), int(minute)
        if h < 0 or h > 23 or m < 0 or m > 59:
            raise ValueError(f"Invalid time value: {value}")


def schedule_jobs(config: AppConfig) -> None:
    validate_schedule(config.schedule)
    tz = ZoneInfo(config.schedule.timezone)
    scheduler = BlockingScheduler(timezone=tz)

    day_expr = ",".join(DAY_MAP[d] for d in config.schedule.days)

    for hhmm in config.schedule.times:
        hour, minute = hhmm.split(":")
        scheduler.add_job(
            run_with_retry,
            "cron",
            day_of_week=day_expr,
            hour=int(hour),
            minute=int(minute),
            args=[config],
            id=f"job-{hhmm}",
            replace_existing=True,
        )
        logging.info("Scheduled job: days=%s time=%s", day_expr, hhmm)

    logging.info("Scheduler started")
    scheduler.start()


def main() -> None:
    setup_logging()
    config = load_config(CONFIG_PATH)

    if len(sys.argv) > 1 and sys.argv[1] == "--run-now":
        run_with_retry(config)
        return

    schedule_jobs(config)


if __name__ == "__main__":
    main()
