from datetime import datetime
from functools import wraps
from time import sleep

from django import db
from django.core.management.base import BaseCommand
from django.db.utils import InterfaceError as DjangoInterfaceError
from psycopg2._psycopg import InterfaceError as Psycopg2InterfaceError

from dimagi.utils.couch import release_lock
from dimagi.utils.couch.cache.cache_core import get_redis_client, RedisClientError
from dimagi.utils.logging import notify_exception


def retry_on_connection_failure(fn):
    @wraps(fn)
    def _inner(*args, **kwargs):
        retry = kwargs.pop('retry', True)
        try:
            return fn(*args, **kwargs)
        except db.utils.DatabaseError:
            # we have to do this manually to avoid issues with
            # open transactions and already closed connections
            db.transaction.rollback()
            # re raise the exception for additional error handling
            raise
        except (Psycopg2InterfaceError, DjangoInterfaceError):
            # force closing the connection to prevent Django from trying to reuse it.
            # http://www.tryolabs.com/Blog/2014/02/12/long-time-running-process-and-django-orm/
            db.connection.close()
            if retry:
                _inner(retry=False, *args, **kwargs)
            else:
                # re raise the exception for additional error handling
                raise

    return _inner


class GenericEnqueuingOperation(BaseCommand):
    """
    Implements a generic enqueuing operation.
    """

    def get_fetching_interval(self):
        return 15

    def handle(self, **options):
        if self.use_queue():
            self.validate_args(**options)
            self.keep_fetching_items()
        else:
            # If we return right away, supervisor will keep trying to restart
            # the service. So just loop and do nothing.
            while True:
                sleep(60)

    def keep_fetching_items(self):
        while True:
            try:
                self.populate_queue()
            except RedisClientError:
                notify_exception(None,
                    message="Could not get redis cache. Is redis configured?")
            except:
                notify_exception(None,
                    message="Could not populate %s." % self.get_queue_name())
            sleep(self.get_fetching_interval())

    @retry_on_connection_failure
    def populate_queue(self):
        client = get_redis_client()
        utcnow = datetime.utcnow()
        entries = self.get_items_to_be_processed(utcnow)
        for entry in entries:
            item_id = entry["id"]
            process_datetime_str = entry["key"]
            self.enqueue(item_id, process_datetime_str, redis_client=client)

    def enqueue(self, item_id, process_datetime_str, redis_client=None):
        client = redis_client or get_redis_client()
        queue_name = self.get_queue_name()
        enqueuing_lock = self.get_enqueuing_lock(client,
            "%s-enqueuing-%s-%s" % (queue_name, item_id, process_datetime_str))
        if enqueuing_lock.acquire(blocking=False):
            try:
                self.enqueue_item(item_id)
            except:
                # We couldn't enqueue, so release the lock
                release_lock(enqueuing_lock, True)

    def get_enqueuing_lock(self, client, key):
        lock_timeout = self.get_enqueuing_timeout() * 60
        return client.lock(key, timeout=lock_timeout)

    def get_queue_name(self):
        """Should return the name of this queue. Used for acquiring the
        enqueuing lock to prevent enqueuing the same item twice"""
        raise NotImplementedError("This method must be implemented.")

    def get_enqueuing_timeout(self):
        """Should return the timeout, in minutes, to use with the
        enqueuing lock. This is essentially the number of minutes to
        wait before enqueuing an unprocessed item again."""
        raise NotImplementedError("This method must be implemented.")

    def get_items_to_be_processed(self, utcnow):
        """Should return the couch query result containing the items to be
        enqueued. The result should just have the id of the item to be 
        processed and the key from the couch view for each item. The couch 
        view should emit a single value, which should be the timestamp that
        the item should be processed. Since this just returns ids and keys, 
        no limiting is necessary.
        utcnow - The current timestamp, in utc, at the time of the method's
            call. Retrieve all items to be processed before this timestamp."""
        raise NotImplementedError("This method must be implemented.")

    def enqueue_item(self, _id):
        """This method should enqueue the item.
        _id - The couch document _id of the item that is being referenced."""
        raise NotImplementedError("This method must be implemented.")

    def use_queue(self):
        """If this is False, the handle() method will do nothing and return."""
        return True

    def validate_args(self, **options):
        """Validate the options passed at the command line."""
        pass
