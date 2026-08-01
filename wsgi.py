"""WSGI entrypoint. Run with: gunicorn --workers 1 --threads 8 wsgi:app

Importing app:app directly would skip bootstrap() and leave the process with an
unmigrated database and no Telegram poller, so production servers must target
this module instead.
"""
# `app` is re-exported, not unused: gunicorn is pointed at wsgi:app and imports it
# from here. Removing it would break the deployment and nothing else would notice.
from app import app, bootstrap  # noqa: F401

bootstrap()
