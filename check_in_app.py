import json
import logging
import random
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CONFIG_PATH = Path('config.json')
LOG_DIR = Path('logs')
CAPTCHA_DIR = Path('runtime') / 'captcha'
SCHEDULE_FILE = Path('runtime') / 'schedule.json'
TIMEZONE = ZoneInfo('Asia/Taipei')
DAY_LABELS = ['一', '二', '三', '四', '五', '六', '日']
CHECK_IN_RANGE = ((7, 30), (7, 50))
CHECK_OUT_RANGE = ((17, 30), (17, 50))
VERIFY_CAPTCHA_ONLY = False
_captcha_ocr = None
_captcha_ocr_beta = None

@dataclass
class SelectorConfig:
    username: str
    password: str
    captcha_input: str
    login_submit: str
    target_button: str
    sign_in_button: str | None = None
    sign_out_button: str | None = None
    captcha_image: str | None = None
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
    site: SiteConfig
    selectors: SelectorConfig
    timing: TimingConfig
    retry: RetryConfig
    captcha: CaptchaConfig
    line: LineConfig


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', handlers=[logging.FileHandler(LOG_DIR / f'run-{datetime.now():%Y%m%d}.log', encoding='utf-8'), logging.StreamHandler()], force=True)


def load_config() -> AppConfig:
    raw = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    return AppConfig(SiteConfig(**raw['site']), SelectorConfig(**raw['selectors']), TimingConfig(**raw['timing']), RetryConfig(**raw['retry']), CaptchaConfig(**raw.get('captcha', {})), LineConfig(**raw.get('line', {})))


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    value = value.strip().lower()
    return not value or value.startswith('your_') or value in {'username', 'password', 'your_account', 'yourpassword'}


def captcha_image(page, config: AppConfig) -> Path:
    path = CAPTCHA_DIR / 'current.png'
    if config.selectors.captcha_image:
        page.locator(config.selectors.captcha_image).first.screenshot(path=str(path))
    else:
        page.screenshot(path=str(path), full_page=True)
    return path


def ocr_captcha(page, config: AppConfig) -> str:
    global _captcha_ocr, _captcha_ocr_beta
    if not config.selectors.captcha_image:
        raise ValueError('auto 模式需要設定 selectors.captcha_image')
    import ddddocr
    image = captcha_image(page, config).read_bytes()
    candidates = []
    for beta in (False, True):
        try:
            if beta:
                _captcha_ocr_beta = _captcha_ocr_beta or ddddocr.DdddOcr(beta=True, show_ad=False)
                text = _captcha_ocr_beta.classification(image).strip()
            else:
                _captcha_ocr = _captcha_ocr or ddddocr.DdddOcr(show_ad=False)
                text = _captcha_ocr.classification(image).strip()
            logging.info('Captcha OCR: %s', text)
            if re.fullmatch(r'[A-Za-z0-9]{4,8}', text):
                candidates.append(text)
        except Exception as error:
            logging.debug('Captcha OCR failed: %s', error)
    if candidates:
        return Counter(candidates).most_common(1)[0][0]
    raise ValueError('自動辨識驗證碼失敗')


def line_captcha(page, config: AppConfig) -> str:
    image_path = captcha_image(page, config)
    request_time = datetime.now()
    image_url = f"{config.line.public_base_url.rstrip('/')}/{image_path.name}?t={int(request_time.timestamp())}"
    response = requests.post('https://api.line.me/v2/bot/message/push', headers={'Authorization': f'Bearer {config.line.channel_access_token}', 'Content-Type': 'application/json'}, json={'to': config.line.user_id, 'messages': [{'type': 'image', 'originalContentUrl': image_url, 'previewImageUrl': image_url}]}, timeout=15)
    response.raise_for_status()
    deadline = time.time() + config.captcha.wait_seconds
    inbox = Path(config.line.inbox_file)
    while time.time() < deadline:
        if inbox.exists():
            for line in reversed(inbox.read_text(encoding='utf-8').splitlines()):
                try:
                    item = json.loads(line)
                    if datetime.fromisoformat(item.get('created_at', '')) >= request_time:
                        text = str(item.get('text', '')).strip()
                        if text:
                            return text
                except (ValueError, json.JSONDecodeError):
                    pass
        time.sleep(max(config.captcha.poll_seconds, 1))
    raise TimeoutError('LINE captcha response timed out')


def get_captcha(page, config: AppConfig) -> str:
    mode = config.captcha.input_mode.strip().lower()
    if mode == 'auto':
        return ocr_captcha(page, config)
    if mode == 'manual':
        value = input('請輸入瀏覽器中的 CAPTCHA：').strip()
        if not value:
            raise ValueError('Captcha cannot be empty')
        return value
    if mode == 'line':
        if not config.line.enabled or not config.line.channel_access_token or not config.line.user_id or not config.line.public_base_url:
            raise ValueError('LINE settings are incomplete')
        return line_captcha(page, config)
    raise ValueError(f'Unsupported captcha input mode: {config.captcha.input_mode}')


def execute_action(config: AppConfig, selector: str) -> None:
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, config.site.browser)
        browser = browser_type.launch(headless=config.site.headless, args=['--window-position=0,0', '--window-size=1280,900'])
        context = browser.new_context(no_viewport=True)
        page = context.new_page()
        try:
            page.goto(config.site.login_url, wait_until='domcontentloaded', timeout=config.timing.navigation_timeout_ms)
            for attempt in range(3):
                username = page.locator(config.selectors.username)
                password = page.locator(config.selectors.password)
                if is_placeholder(config.site.username) or is_placeholder(config.site.password):
                    username.click()
                    password.click()
                else:
                    username.fill(config.site.username)
                    password.fill(config.site.password)
                if config.selectors.captcha_input:
                    page.fill(config.selectors.captcha_input, get_captcha(page, config))
                    if VERIFY_CAPTCHA_ONLY:
                        while True:
                            page.wait_for_timeout(1000)
                page.click(config.selectors.login_submit)
                if not config.selectors.post_login_ready:
                    break
                try:
                    page.wait_for_selector(config.selectors.post_login_ready, timeout=config.timing.wait_timeout_ms)
                    break
                except PlaywrightTimeoutError:
                    if attempt == 2:
                        raise
                    page.reload(wait_until='domcontentloaded', timeout=config.timing.navigation_timeout_ms)
            page.click(selector)
            if config.selectors.target_done:
                page.wait_for_selector(config.selectors.target_done, timeout=config.timing.wait_timeout_ms)
        finally:
            context.close()
            browser.close()


def run_action(config: AppConfig, selector: str) -> None:
    last_error = None
    for attempt in range(config.retry.attempts):
        try:
            execute_action(config, selector)
            return
        except (PlaywrightTimeoutError, ValueError, RuntimeError, TimeoutError) as error:
            last_error = error
            logging.error('Action failed: %s', error)
            if attempt + 1 < config.retry.attempts:
                time.sleep(config.retry.wait_seconds)
    if last_error:
        raise last_error


def random_time(time_range: tuple[tuple[int, int], tuple[int, int]]) -> str:
    start, end = time_range
    value = random.randint(start[0] * 60 + start[1], end[0] * 60 + end[1])
    return f'{value // 60:02d}:{value % 60:02d}'


def save_schedule(rows: list[dict[str, Any]]) -> None:
    SCHEDULE_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')


def load_schedule() -> list[dict[str, Any]]:
    saved = {}
    if SCHEDULE_FILE.exists():
        try:
            saved = {row['date']: row for row in json.loads(SCHEDULE_FILE.read_text(encoding='utf-8'))}
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    today = datetime.now(TIMEZONE).date()
    rows = []
    for offset in range(7):
        current = today + timedelta(days=offset)
        key = current.isoformat()
        old = saved.get(key, {})
        rows.append({'date': key, 'weekday': DAY_LABELS[current.weekday()], 'check_in': old.get('check_in', random_time(CHECK_IN_RANGE)), 'check_out': old.get('check_out', random_time(CHECK_OUT_RANGE)), 'enabled': bool(old.get('enabled', current.weekday() < 5))})
    save_schedule(rows)
    return rows


def valid_time(value: str) -> bool:
    return bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value.strip()))


def start_gui(config: AppConfig) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title('何帥簽到退系統')
    root.geometry('560x500')
    root.minsize(520, 460)
    root.option_add('*Font', ('Microsoft JhengHei UI', 12))
    style = ttk.Style(root)
    style.configure('Header.TLabel', font=('Microsoft JhengHei UI', 13, 'bold'), background='#285197', foreground='white')
    style.configure('Date.TLabel', font=('Microsoft JhengHei UI', 12), anchor='center')
    style.configure('Action.TButton', font=('Microsoft JhengHei UI', 14, 'bold'), padding=(8, 10))
    style.configure('Save.TButton', font=('Microsoft JhengHei UI', 13, 'bold'), padding=(8, 8))
    style.configure('Status.TLabel', font=('Microsoft JhengHei UI', 12))
    executor = ThreadPoolExecutor(max_workers=2)
    rows = load_schedule()
    fired: set[tuple[str, str]] = set()
    widgets = []
    status = tk.StringVar(value='系統就緒')
    table = ttk.Frame(root)
    table.pack(fill='x', padx=12, pady=(12, 0))
    actions = ttk.Frame(root)
    actions.pack(fill='x', padx=12, pady=10)
    sign_in = config.selectors.sign_in_button or 'button:has-text("上班簽到")'
    sign_out = config.selectors.sign_out_button or config.selectors.target_button

    def action(selector: str, label: str) -> None:
        status.set(f'{label}執行中...')
        future = executor.submit(run_action, config, selector)
        def done(result) -> None:
            try:
                result.result()
                status.set(f'{label}完成')
            except Exception:
                status.set(f'{label}失敗，請查看 logs')
        future.add_done_callback(done)

    ttk.Button(actions, text='簽到', style='Action.TButton', width=7, command=lambda: action(sign_in, '簽到')).pack(side='left', padx=(0, 8))
    ttk.Button(actions, text='簽退', style='Action.TButton', width=7, command=lambda: action(sign_out, '簽退')).pack(side='right', padx=(8, 0))
    ttk.Label(root, textvariable=status, style='Status.TLabel').pack(pady=5)

    def save_from_widgets() -> bool:
        for check_in, check_out, enabled, row in widgets:
            if not valid_time(check_in.get()) or not valid_time(check_out.get()):
                messagebox.showerror('時間格式錯誤', '請使用 HH:MM 格式，例如 07:30。')
                return False
            row.update(check_in=check_in.get().strip(), check_out=check_out.get().strip(), enabled=enabled.get())
        save_schedule(rows)
        return True

    def render() -> None:
        nonlocal rows, widgets
        if widgets:
            save_from_widgets()
        rows = load_schedule()
        widgets = []
        for child in table.winfo_children():
            child.destroy()
        for column, label in enumerate(('日期', '簽到時間', '簽退時間', '自動執行')):
            ttk.Label(table, text=label, style='Header.TLabel', anchor='center').grid(row=0, column=column, sticky='nsew', padx=1, pady=1)
            table.columnconfigure(column, weight=1)
        for index, row in enumerate(rows, 1):
            current = date.fromisoformat(row['date'])
            ttk.Label(table, text=f'{current.month}/{current.day}（{row["weekday"]}）', style='Date.TLabel').grid(row=index, column=0, sticky='nsew', padx=1, pady=1)
            check_in = tk.StringVar(value=row['check_in'])
            check_out = tk.StringVar(value=row['check_out'])
            enabled = tk.BooleanVar(value=row['enabled'])
            ttk.Entry(table, textvariable=check_in, justify='center', font=('Microsoft JhengHei UI', 12)).grid(row=index, column=1, sticky='nsew', padx=1, pady=1)
            ttk.Entry(table, textvariable=check_out, justify='center', font=('Microsoft JhengHei UI', 12)).grid(row=index, column=2, sticky='nsew', padx=1, pady=1)
            ttk.Checkbutton(table, text='ON', variable=enabled).grid(row=index, column=3, sticky='nsew', padx=1, pady=1)
            widgets.append((check_in, check_out, enabled, row))

    previous_day = datetime.now(TIMEZONE).date()
    def tick() -> None:
        nonlocal previous_day
        now = datetime.now(TIMEZONE)
        if now.date() != previous_day:
            previous_day = now.date()
            render()
        current_key = now.date().isoformat()
        current_time = now.strftime('%H:%M')
        for check_in, check_out, enabled, row in widgets:
            if enabled.get() and row['date'] == current_key:
                for kind, scheduled, selector, label in (('in', check_in.get(), sign_in, '自動簽到'), ('out', check_out.get(), sign_out, '自動簽退')):
                    key = (current_key, kind)
                    if scheduled == current_time and key not in fired:
                        fired.add(key)
                        action(selector, label)
        root.after(1000, tick)

    ttk.Button(actions, text='儲存設定', style='Save.TButton', command=save_from_widgets).pack(side='left', expand=True, fill='x', padx=8)
    render()
    tick()
    root.protocol('WM_DELETE_WINDOW', lambda: (save_from_widgets(), executor.shutdown(wait=False, cancel_futures=True), root.destroy()))
    root.mainloop()


def main() -> None:
    setup_logging()
    config = load_config()
    start_gui(config)


if __name__ == '__main__':
    main()
