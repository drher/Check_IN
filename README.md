# Timed Browser Login Clicker

This project opens a browser at fixed times, logs into a website, and clicks a target button.

Captcha handling supports two modes:
- `manual`: type captcha in terminal.
- `line`: script sends captcha image to LINE. You reply with text, script reads it and fills automatically.

## 1) Setup

```powershell
cd .
pip install -r requirements.txt
playwright install chromium
```

Edit `config.json`:
- `site.login_url`, `site.username`, `site.password`
- `selectors.*` must match your real page elements
- `schedule.times` and `schedule.days`
- `captcha.input_mode` = `manual` or `line`

## 2) LINE mode architecture

In `line` mode, the flow is:
1. Save captcha screenshot to `runtime/captcha/current.png`
2. Send LINE push message containing an image URL
3. You reply in LINE
4. Webhook receives your text and appends it to `runtime/line_inbox.jsonl`
5. Main script polls this file and fills captcha automatically

## 3) Configure LINE mode

Required `config.json` fields in `line` section:
- `channel_access_token`: from LINE Messaging API
- `user_id`: your LINE user id
- `public_base_url`: a public URL that can expose `runtime/captcha/current.png`
- `inbox_file`: default `runtime/line_inbox.jsonl`

### 3.1 Start webhook receiver

```powershell
python line_webhook.py
```

Webhook URL should be:
`https://<your-public-domain>/callback`

### 3.2 Expose local service to internet

Use a tunnel service, for example ngrok:

```powershell
ngrok http 8787
```

Then set LINE webhook URL to `https://<ngrok-domain>/callback`.

### 3.3 Expose captcha image path

`public_base_url` must host file path `/captcha/current.png`.

One practical way:
- run a static file server for `runtime/`
- map it to `https://<your-domain>/captcha/current.png`

If your static server maps root directly to `runtime/`, `current.png` can be served by:
`https://<your-domain>/captcha/current.png`

## 4) 執行模式

### 自動執行

直接啟動程式，會在星期一至星期五每天 07:30 執行一次：

在 VS Code 開啟 `check_in.ipynb`，執行第 6 格自動排程 cell。

程式需要保持執行中，才能在排程時間啟動任務。

### 手動執行（開發測試）

使用 `--run-now` 立即執行一次，不會啟動常駐排程：

在 VS Code 開啟 `check_in.ipynb`，執行第 4 格手動測試 cell。

## 5) How to find selectors

1. Open Chrome/Edge Developer Tools (F12).
2. Use element picker and inspect each input/button.
3. Fill values in `config.json`.

## Notes

- Logs are stored in `logs/`.
- On error, screenshot is saved in `logs/`.
- If you want no visible browser, set `site.headless` to `true`.
