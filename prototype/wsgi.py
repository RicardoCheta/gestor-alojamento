"""
Production WSGI entrypoint — one web service serves both apps:

    /            -> host dashboard + admin console   (dashboard.py)
    /hooks/...   -> Twilio WhatsApp webhook          (webhook.py)

So Twilio's "When a message comes in" URL is  https://<host>/hooks/whatsapp

Run with:  gunicorn wsgi:app

The proactive-message scheduler is NOT started here — run it as a separate
cron job:  python scheduler.py   (every ~15 min). See docs/deploy.md.
"""

from werkzeug.middleware.dispatcher import DispatcherMiddleware

import dashboard
import webhook

# Each app already applies ProxyFix to itself (needed behind Render/Railway TLS).
app = DispatcherMiddleware(dashboard.app, {"/hooks": webhook.app})
