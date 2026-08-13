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

## 4) Run once (for testing)

```powershell
python main.py --run-now
```

## 5) Run as scheduler process

```powershell
python main.py
```

Keep the process running. It will execute at the configured time.

## 6) How to find selectors

1. Open Chrome/Edge Developer Tools (F12).
2. Use element picker and inspect each input/button.
3. Fill values in `config.json`.

## Notes

- Logs are stored in `logs/`.
- On error, screenshot is saved in `logs/`.
- If you want no visible browser, set `site.headless` to `true`.
