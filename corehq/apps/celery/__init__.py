from celery import Celery
from django.apps import AppConfig


class Config(AppConfig):
    """Configure global Celery app as part of Django setup"""
    name = 'corehq.apps.celery'

    def __init__(self, *args, **kw):
        _init_celery_app()
        super().__init__(*args, **kw)


def _init_celery_app():
    assert "app" not in globals(), "Celery is already initialized"
    global app
    app = Celery()
    app.config_from_object('django.conf:settings', namespace='CELERY')
    app.autodiscover_tasks()
    app.set_default()
