import uuid
from datetime import datetime

from django.test import SimpleTestCase, TestCase

from decorator import contextmanager

from casexml.apps.phone.document_store import SyncLogDocumentStore
from corehq.apps.domain.tests.test_utils import test_domain
from dimagi.utils.couch.database import get_db
from pillowtop.dao.couch import CouchDocumentStore

from corehq.apps.change_feed import data_sources
from corehq.apps.change_feed.data_sources import get_document_store
from corehq.apps.change_feed.exceptions import UnknownDocumentStore
from corehq.apps.locations.document_store import LocationDocumentStore
from corehq.apps.sms.document_stores import SMSDocumentStore
from corehq.form_processor.backends.sql.dbaccessors import LedgerAccessorSQL
from corehq.form_processor.document_stores import (
    CaseDocumentStore,
    DocStoreLoadTracker,
    FormDocumentStore,
    LedgerV2DocumentStore,
)
from corehq.form_processor.models import CommCareCase, XFormInstance
from corehq.form_processor.tests.utils import sharded
from corehq.util.exceptions import DatabaseNotFound
from corehq.util.test_utils import generate_cases


class DocumentStoreTests(SimpleTestCase):
    def test_missing_db(self):
        with self.assertRaises(DatabaseNotFound):
            get_document_store(data_sources.SOURCE_COUCH, 'baddb', 'domain')

    def test_unknown_store(self):
        with self.assertRaises(UnknownDocumentStore):
            get_document_store(data_sources.SOURCE_SQL, 'badsource', 'domain')


@generate_cases([
    (data_sources.SOURCE_COUCH, 'test_commcarehq', CouchDocumentStore),

    # legacy
    (data_sources.CASE_SQL, '', CaseDocumentStore),
    (data_sources.FORM_SQL, '', FormDocumentStore),
    (data_sources.LEDGER_V2, '', LedgerV2DocumentStore),
    (data_sources.LOCATION, '', LocationDocumentStore),
    (data_sources.SYNCLOG_SQL, '', SyncLogDocumentStore),
    (data_sources.SMS, '', SMSDocumentStore),

    (data_sources.SOURCE_SQL, data_sources.CASE_SQL, CaseDocumentStore),
    (data_sources.SOURCE_SQL, data_sources.FORM_SQL, FormDocumentStore),
    (data_sources.SOURCE_SQL, data_sources.LEDGER_V2, LedgerV2DocumentStore),
    (data_sources.SOURCE_SQL, data_sources.LOCATION, LocationDocumentStore),
    (data_sources.SOURCE_SQL, data_sources.SYNCLOG_SQL, SyncLogDocumentStore),
    (data_sources.SOURCE_SQL, data_sources.SMS, SMSDocumentStore),

], DocumentStoreTests)
def test_get_document_store(self, source_type, source_name, expected):
    store = get_document_store(source_type, source_name, 'domain')
    if isinstance(store, DocStoreLoadTracker):
        store = store.store
    self.assertEqual(store.__class__, expected)


@contextmanager
def couch_data():
    db = get_db()
    docs = [
        {'doc_type': 'doc_type', 'domain': 'domain'}
        for i in range(3)
    ]
    for doc in docs:
        db.save_doc(doc)

    try:
        yield [doc['_id'] for doc in docs]
    finally:
        db.delete_docs(docs)


@contextmanager
def case_form_data():
    from casexml.apps.case.mock import CaseFactory
    factory = CaseFactory('domain')
    cases = []
    forms = []
    for i in range(3):
        case_id = uuid.uuid4().hex
        case_block = factory.get_case_block(case_id, case_type='case_type')
        form, [case] = factory.post_case_blocks([case_block])
        cases.append(case)
        forms.append(form)

    case_ids = [case.case_id for case in cases]
    form_ids = [form.form_id for form in forms]

    try:
        yield form_ids, case_ids
    finally:
        XFormInstance.objects.hard_delete_forms('domain', form_ids)
        CommCareCase.objects.hard_delete_cases('domain', case_ids)


@contextmanager
def case_data():
    with case_form_data() as (form_ids, case_ids):
        yield case_ids


@contextmanager
def form_data():
    with case_form_data() as (form_ids, case_ids):
        yield form_ids


@contextmanager
def location_data():
    from corehq.apps.locations.tests.util import LocationTypeStructure
    from corehq.apps.locations.tests.util import LocationStructure
    from corehq.apps.locations.tests.util import setup_location_types_with_structure
    from corehq.apps.locations.tests.util import setup_locations_with_structure
    location_type_structure = [LocationTypeStructure('t1', [])]

    location_structure = [
        LocationStructure('L1', 't1', []),
        LocationStructure('L2', 't1', []),
        LocationStructure('L3', 't1', []),
    ]

    with test_domain():
        setup_location_types_with_structure('domain', location_type_structure)
        locs = setup_locations_with_structure('domain', location_structure)

        yield [loc.location_id for name, loc in locs.items()]


@contextmanager
def ledger_data():
    from casexml.apps.stock.mock import Balance
    from casexml.apps.case.mock import CaseFactory
    from casexml.apps.stock.mock import Entry

    factory = CaseFactory('domain')
    with case_data() as case_ids:
        balance_blocks = [
            Balance(
                entity_id=case_id,
                date=datetime.utcnow(),
                section_id='test',
                entry=Entry(id='chocolate', quantity=4),
            ).as_xml()
            for case_id in case_ids
        ]
        form, _ = factory.post_case_blocks(balance_blocks)

        ledgers = LedgerAccessorSQL.get_ledger_values_for_cases(case_ids)

        try:
            yield [ledger.ledger_id for ledger in ledgers]
        finally:
            form.delete()
            for ledger in ledgers:
                ledger.delete()


@contextmanager
def synclog_data():
    from casexml.apps.phone.models import SimplifiedSyncLog
    synclogs = [
        SimplifiedSyncLog(domain='domain', user_id=uuid.uuid4().hex, date=datetime.utcnow())
        for i in range(3)
    ]
    for synclog in synclogs:
        synclog.save()

    try:
        yield [synclog.get_id for synclog in synclogs]
    finally:
        for synclog in synclogs:
            synclog.delete()


@contextmanager
def sms_data():
    from corehq.apps.sms.models import SMS
    sms_objects = [
        SMS.objects.create(domain='domain')
        for i in range(3)
    ]

    try:
        yield [sms.couch_id for sms in sms_objects]
    finally:
        for sms in sms_objects:
            sms.delete()


@sharded
class DocumentStoreDbTests(TestCase):
    pass


def _test_document_store(self, doc_store_cls, doc_store_args, data_context, id_field):
    doc_store = doc_store_cls(*doc_store_args)
    with data_context() as doc_ids:
        self.assertIsNotNone(doc_store.get_document(doc_ids[0]))

        self.assertEqual(set(doc_ids), set(doc_store.iter_document_ids()))

        docs = doc_store.iter_documents(doc_ids[1:])
        self.assertEqual(set(doc_ids[1:]), {doc[id_field] for doc in docs})


@generate_cases([
    (CouchDocumentStore, (get_db(), 'domain', 'doc_type'), couch_data, '_id'),
    (CaseDocumentStore, ('domain',), case_data, '_id'),
    (FormDocumentStore, ('domain',), form_data, '_id'),
    (LocationDocumentStore, ('domain',), location_data, 'location_id'),
    (LedgerV2DocumentStore, ('domain',), ledger_data, '_id'),
    (SyncLogDocumentStore, (), synclog_data, '_id'),
    (SMSDocumentStore, (), sms_data, '_id'),
], DocumentStoreDbTests)
def test_documet_store(*args):
    _test_document_store(*args)
