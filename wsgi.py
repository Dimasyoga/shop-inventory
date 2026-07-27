"""WSGI entrypoint. Run with: gunicorn --workers 1 --threads 8 wsgi:app

Importing app:app directly would skip bootstrap() and leave the process with an
unmigrated database and no Telegram poller, so production servers must target
this module instead.
"""
from app import app, bootstrap

bootstrap()
