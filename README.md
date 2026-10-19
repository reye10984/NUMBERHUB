# NumberHub

Small Flask + SQLite web app (mobile friendly): registration/login, hashed passwords, wallet ledger
(balance computed server-side), number requests, history, SMS inbox, provider status page,
health check and an Africa's Talking inbound-SMS webhook.

## Run locally
```
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # edit values; never commit .env
export $(grep -v '^#' .env | xargs)
python app.py               # http://localhost:5000
python -m unittest discover -s tests -v
```

## Modes
- **demo** (`DEMO_MODE=1`, default): no credentials needed. Numbers look like `DEMO-123456`; credit and
  messages are simulated and labelled DEMO. Nothing real is provisioned or received.
- **live** (`DEMO_MODE=0` plus `AT_USERNAME`, `AT_API_KEY`, `AT_NUMBER`, `WEBHOOK_SECRET`): the webhook accepts
  real inbound SMS from Africa's Talking. "Request number" assigns the single number in `AT_NUMBER`
  (one user only); the app does not buy numbers automatically. Add numbers in your Africa's Talking dashboard.
- **unconfigured** (`DEMO_MODE=0`, variables missing): requests are blocked with a clear message.

## Africa's Talking setup
In the dashboard, set the SMS inbound callback URL to
`https://YOUR-HOST/webhook/sms?secret=YOUR_WEBHOOK_SECRET`. Requests with a wrong secret get 403.
Credentials are read only from environment variables and are never displayed.

## Deploy (Render / Railway / Heroku style)
Uses `Procfile` (`gunicorn app:app`). Set environment variables in the host dashboard, especially a fixed
`SECRET_KEY`, and `DATABASE_PATH` on a persistent disk. SQLite suits a single instance only.

## Not included (v1)
Real payments/top-ups, outbound SMS, password reset, login rate limiting, admin tools. Do not use this app
to receive verification codes for accounts that are not yours.
