import contextlib
import uuid

from django.db.utils import IntegrityError, InternalError
from django.test import TestCase, TransactionTestCase
from unittest.mock import patch

from casexml.apps.case.mock import CaseBlock, CaseFactory, CaseStructure
from casexml.apps.case.util import post_case_blocks
from corehq.apps.hqcase.utils import submit_case_blocks
from corehq.apps.products.models import SQLProduct
from corehq.apps.receiverwrapper.util import submit_form_locally
from corehq.form_processor.exceptions import CaseNotFound, XFormNotFound
from corehq.form_processor.interfaces.dbaccessors import LedgerAccessors
from corehq.form_processor.models import CommCareCase, XFormInstance
from corehq.form_processor.reprocess import (
    reprocess_form,
    reprocess_unfinished_stub,
    reprocess_xform_error,
)
from corehq.form_processor.signals import sql_case_post_save
from corehq.form_processor.tests.utils import (
    FormProcessorTestUtils,
    sharded,
)
from corehq.util.context_managers import catch_signal
from couchforms.models import UnfinishedSubmissionStub
from couchforms.signals import successful_form_received


@sharded
class ReprocessXFormErrorsTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super(ReprocessXFormErrorsTest, cls).setUpClass()

        cls.domain = uuid.uuid4().hex

    @classmethod
    def tearDownClass(cls):
        FormProcessorTestUtils.delete_all_cases_forms_ledgers(cls.domain)
        super(ReprocessXFormErrorsTest, cls).tearDownClass()

    def test_reprocess_xform_error(self):
        case_id = uuid.uuid4().hex
        parent_case_id = uuid.uuid4().hex
        case = CaseBlock(
            create=True,
            case_id=case_id,
            user_id='user1',
            owner_id='user1',
            case_type='demo',
            case_name='child',
            index={'parent': ('parent_type', parent_case_id)}
        )

        post_case_blocks([case.as_xml()], domain=self.domain)

        get_forms_by_type = XFormInstance.objects.get_forms_by_type
        error_forms = get_forms_by_type(self.domain, 'XFormError', 10)
        self.assertEqual(1, len(error_forms))

        form = error_forms[0]
        reprocess_xform_error(form)
        error_forms = get_forms_by_type(self.domain, 'XFormError', 10)
        self.assertEqual(1, len(error_forms))

        case = CaseBlock(
            create=True,
            case_id=parent_case_id,
            user_id='user1',
            owner_id='user1',
            case_type='parent_type',
            case_name='parent',
        )

        post_case_blocks([case.as_xml()], domain=self.domain)

        reprocess_xform_error(XFormInstance.objects.get_form(form.form_id))

        form = XFormInstance.objects.get_form(form.form_id)
        # self.assertTrue(form.initial_processing_complete)  Can't change this with SQL forms at the moment
        self.assertTrue(form.is_normal)
        self.assertIsNone(form.problem)

        case = CommCareCase.objects.get_case(case_id, self.domain)
        self.assertEqual(1, len(case.indices))
        self.assertEqual(case.indices[0].referenced_id, parent_case_id)
        self._validate_case(case)

    def _validate_case(self, case):
        self.assertEqual(1, len(case.transactions))
        self.assertTrue(case.transactions[0].is_form_transaction)
        self.assertTrue(case.transactions[0].is_case_create)
        self.assertTrue(case.transactions[0].is_case_index)
        self.assertFalse(case.transactions[0].revoked)


@sharded
class ReprocessSubmissionStubTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super(ReprocessSubmissionStubTests, cls).setUpClass()
        cls.domain = uuid.uuid4().hex
        cls.product = SQLProduct.objects.create(domain=cls.domain, product_id='product1', name='product1')

    @classmethod
    def tearDownClass(cls):
        cls.product.delete()
        super(ReprocessSubmissionStubTests, cls).tearDownClass()

    def setUp(self):
        super(ReprocessSubmissionStubTests, self).setUp()
        self.factory = CaseFactory(domain=self.domain)
        self.formdb = XFormInstance.objects
        self.ledgerdb = LedgerAccessors(self.domain)

    def tearDown(self):
        FormProcessorTestUtils.delete_all_cases_forms_ledgers(self.domain)
        super(ReprocessSubmissionStubTests, self).tearDown()

    def test_reprocess_unfinished_submission_case_create(self):
        case_id = uuid.uuid4().hex
        with _patch_save_to_raise_error(self):
            self.factory.create_or_update_cases([
                CaseStructure(case_id=case_id, attrs={'case_type': 'parent', 'create': True})
            ])

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(1, len(stubs))

        # form that was saved before case error raised
        normal_form_ids = XFormInstance.objects.get_form_ids_in_domain(self.domain, 'XFormInstance')
        self.assertEqual(0, len(normal_form_ids))

        # shows error form (duplicate of form that was saved before case error)
        # this is saved becuase the saving was assumed to be atomic so if there was any error it's assumed
        # the form didn't get saved
        # we don't really care about this form in this test
        error_forms = XFormInstance.objects.get_forms_by_type(self.domain, 'XFormError', 10)
        self.assertEqual(1, len(error_forms))
        self.assertIsNone(error_forms[0].orig_id)
        self.assertEqual(error_forms[0].form_id, stubs[0].xform_id)

        self.assertEqual(0, len(CommCareCase.objects.get_case_ids_in_domain(self.domain)))

        result = reprocess_unfinished_stub(stubs[0])
        self.assertEqual(1, len(result.cases))

        case_ids = CommCareCase.objects.get_case_ids_in_domain(self.domain)
        self.assertEqual(1, len(case_ids))
        self.assertEqual(case_id, case_ids[0])

        with self.assertRaises(UnfinishedSubmissionStub.DoesNotExist):
            UnfinishedSubmissionStub.objects.get(pk=stubs[0].pk)

    def test_reprocess_unfinished_submission_case_update(self):
        case_id = uuid.uuid4().hex
        form_ids = []
        form_ids.append(submit_case_blocks(
            CaseBlock(case_id=case_id, create=True, case_type='box').as_text(),
            self.domain
        )[0].form_id)

        with _patch_save_to_raise_error(self):
            submit_case_blocks(
                CaseBlock(case_id=case_id, update={'prop': 'a'}).as_text(),
                self.domain
            )

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(1, len(stubs))

        form_ids.append(stubs[0].xform_id)

        # submit second form with case update
        form_ids.append(submit_case_blocks(
            CaseBlock(case_id=case_id, update={'prop': 'b'}).as_text(),
            self.domain
        )[0].form_id)

        case = CommCareCase.objects.get_case(case_id, self.domain)
        self.assertEqual(2, len(case.xform_ids))
        self.assertEqual('b', case.get_case_property('prop'))

        result = reprocess_unfinished_stub(stubs[0])
        self.assertEqual(1, len(result.cases))
        self.assertEqual(0, len(result.ledgers))

        case = CommCareCase.objects.get_case(case_id, self.domain)
        self.assertEqual('b', case.get_case_property('prop'))  # should be property value from most recent form
        self.assertEqual(3, len(case.xform_ids))
        self.assertEqual(form_ids, case.xform_ids)

        with self.assertRaises(UnfinishedSubmissionStub.DoesNotExist):
            UnfinishedSubmissionStub.objects.get(pk=stubs[0].pk)

    def test_reprocess_unfinished_submission_ledger_create(self):
        from corehq.apps.commtrack.tests.util import get_single_balance_block
        case_id = uuid.uuid4().hex
        self.factory.create_or_update_cases([
            CaseStructure(case_id=case_id, attrs={'case_type': 'parent', 'create': True})
        ])

        with _patch_save_to_raise_error(self):
            submit_case_blocks(
                get_single_balance_block(case_id, 'product1', 100),
                self.domain
            )

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(1, len(stubs))

        ledgers = self.ledgerdb.get_ledger_values_for_case(case_id)
        self.assertEqual(0, len(ledgers))

        case = CommCareCase.objects.get_case(case_id, self.domain)
        self.assertEqual(1, len(case.xform_ids))

        ledger_transactions = self.ledgerdb.get_ledger_transactions_for_case(case_id)
        self.assertEqual(0, len(ledger_transactions))

        result = reprocess_unfinished_stub(stubs[0])
        self.assertEqual(1, len(result.cases))
        self.assertEqual(1, len(result.ledgers))

        ledgers = self.ledgerdb.get_ledger_values_for_case(case_id)
        self.assertEqual(1, len(ledgers))

        ledger_transactions = self.ledgerdb.get_ledger_transactions_for_case(case_id)
        self.assertEqual(1, len(ledger_transactions))

        # case still only has 2 transactions
        case = CommCareCase.objects.get_case(case_id, self.domain)
        self.assertEqual(2, len(case.xform_ids))
        self.assertTrue(case.actions[1].is_ledger_transaction)

    def test_reprocess_unfinished_submission_ledger_rebuild(self):
        from corehq.apps.commtrack.tests.util import get_single_balance_block
        case_id = uuid.uuid4().hex
        form_ids = []
        form_ids.append(submit_case_blocks(
            [
                CaseBlock(case_id=case_id, create=True, case_type='shop').as_text(),
                get_single_balance_block(case_id, 'product1', 100),
            ],
            self.domain
        )[0].form_id)

        with _patch_save_to_raise_error(self):
            submit_case_blocks(
                get_single_balance_block(case_id, 'product1', 50),
                self.domain
            )

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(1, len(stubs))
        form_ids.append(stubs[0].xform_id)

        # submit another form afterwards
        form_ids.append(submit_case_blocks(
            get_single_balance_block(case_id, 'product1', 25),
            self.domain
        )[0].form_id)

        ledgers = self.ledgerdb.get_ledger_values_for_case(case_id)
        self.assertEqual(1, len(ledgers))
        self.assertEqual(25, ledgers[0].balance)

        ledger_transactions = self.ledgerdb.get_ledger_transactions_for_case(case_id)
        self.assertEqual(2, len(ledger_transactions))

        # should rebuild ledger transactions
        result = reprocess_unfinished_stub(stubs[0])
        self.assertEqual(1, len(result.cases))
        self.assertEqual(1, len(result.ledgers))

        ledgers = self.ledgerdb.get_ledger_values_for_case(case_id)
        self.assertEqual(1, len(ledgers))  # still only 1
        self.assertEqual(25, ledgers[0].balance)

        ledger_transactions = self.ledgerdb.get_ledger_transactions_for_case(case_id)
        self.assertEqual(3, len(ledger_transactions))
        # make sure transactions are in correct order
        self.assertEqual(form_ids, [trans.form_id for trans in ledger_transactions])
        self.assertEqual(100, ledger_transactions[0].updated_balance)
        self.assertEqual(100, ledger_transactions[0].delta)
        self.assertEqual(50, ledger_transactions[1].updated_balance)
        self.assertEqual(-50, ledger_transactions[1].delta)
        self.assertEqual(25, ledger_transactions[2].updated_balance)
        self.assertEqual(-25, ledger_transactions[2].delta)

    def test_fire_signals(self):
        from corehq.apps.receiverwrapper.tests.test_submit_errors import failing_signal_handler
        case_id = uuid.uuid4().hex
        form_id = uuid.uuid4().hex
        with failing_signal_handler('signal death'):
            submit_case_blocks(
                CaseBlock(case_id=case_id, create=True, case_type='box').as_text(),
                self.domain,
                form_id=form_id
            )

        form = self.formdb.get_form(form_id)

        with catch_signal(successful_form_received) as form_handler, \
             catch_signal(sql_case_post_save) as case_handler:
            submit_form_locally(
                instance=form.get_xml(),
                domain=self.domain,
            )

        case = CommCareCase.objects.get_case(case_id, self.domain)

        self.assertEqual(form, form_handler.call_args[1]['xform'])
        self.assertEqual(case, case_handler.call_args[1]['case'])

    def test_reprocess_normal_form(self):
        case_id = uuid.uuid4().hex
        form, cases = submit_case_blocks(
            CaseBlock(case_id=case_id, create=True, case_type='box').as_text(),
            self.domain
        )
        self.assertTrue(form.is_normal)

        result = reprocess_form(form, save=True, lock_form=False)
        self.assertIsNone(result.error)

        case = CommCareCase.objects.get_case(case_id, self.domain)
        transactions = case.actions
        self.assertEqual([trans.form_id for trans in transactions], [form.form_id])

    def test_processing_skipped_when_migrations_are_in_progress(self):
        case_id = uuid.uuid4().hex
        with _patch_save_to_raise_error(self):
            self.factory.create_or_update_cases([
                CaseStructure(case_id=case_id, attrs={'case_type': 'parent', 'create': True})
            ])

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(1, len(stubs))

        with patch('corehq.form_processor.reprocess.any_migrations_in_progress', return_value=True):
            result = reprocess_unfinished_stub(stubs[0])
            self.assertIsNone(result)

        result = reprocess_unfinished_stub(stubs[0])
        self.assertEqual(1, len(result.cases))

    def test_processing_retuns_error_for_missing_form(self):
        case_id = uuid.uuid4().hex
        with _patch_save_to_raise_error(self):
            self.factory.create_or_update_cases([
                CaseStructure(case_id=case_id, attrs={'case_type': 'parent', 'create': True})
            ])

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(1, len(stubs))

        FormProcessorTestUtils.delete_all_cases_forms_ledgers(self.domain)
        with self.assertRaises(XFormNotFound):
            self.formdb.get_form(stubs[0].xform_id)

        result = reprocess_unfinished_stub(stubs[0])
        self.assertIsNotNone(result.error)


@sharded
class TestReprocessDuringSubmission(TestCase):
    @classmethod
    def setUpClass(cls):
        super(TestReprocessDuringSubmission, cls).setUpClass()
        cls.domain = uuid.uuid4().hex

    def setUp(self):
        super(TestReprocessDuringSubmission, self).setUp()
        self.factory = CaseFactory(domain=self.domain)
        self.formdb = XFormInstance.objects
        self.ledgerdb = LedgerAccessors(self.domain)

    def tearDown(self):
        FormProcessorTestUtils.delete_all_cases_forms_ledgers(self.domain)
        super(TestReprocessDuringSubmission, self).tearDown()

    def test_error_saving(self):
        case_id = uuid.uuid4().hex
        form_id = uuid.uuid4().hex
        with _patch_save_to_raise_error(self):
            submit_case_blocks(
                CaseBlock(case_id=case_id, create=True, case_type='box').as_text(),
                self.domain,
                form_id=form_id
            )

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(1, len(stubs))

        form = self.formdb.get_form(form_id)
        self.assertTrue(form.is_error)

        with self.assertRaises(CaseNotFound):
            CommCareCase.objects.get_case(case_id, self.domain)

        result = submit_form_locally(
            instance=form.get_xml(),
            domain=self.domain,
        )
        duplicate_form = result.xform
        self.assertTrue(duplicate_form.is_duplicate)

        case = CommCareCase.objects.get_case(case_id, self.domain)
        self.assertIsNotNone(case)

        form = self.formdb.get_form(form_id)
        self.assertTrue(form.is_normal)
        self.assertIsNone(getattr(form, 'problem', None))
        self.assertEqual(duplicate_form.orig_id, form.form_id)

    def test_processing_error(self):
        case_id = uuid.uuid4().hex
        parent_case_id = uuid.uuid4().hex
        form_id = uuid.uuid4().hex
        form, _ = submit_case_blocks(
            CaseBlock(
                case_id=case_id, create=True, case_type='box',
                index={'cupboard': ('cupboard', parent_case_id)},
            ).as_text(),
            self.domain,
            form_id=form_id
        )

        self.assertTrue(form.is_error)
        self.assertTrue('InvalidCaseIndex' in form.problem)
        self.assertEqual(form.form_id, form_id)

        with self.assertRaises(CaseNotFound):
            CommCareCase.objects.get_case(case_id, self.domain)

        stubs = UnfinishedSubmissionStub.objects.filter(domain=self.domain, saved=False).all()
        self.assertEqual(0, len(stubs))

        # create parent case
        submit_case_blocks(
            CaseBlock(case_id=parent_case_id, create=True, case_type='cupboard').as_text(),
            self.domain,
        )

        # re-submit form
        result = submit_form_locally(
            instance=form.get_xml(),
            domain=self.domain,
        )
        duplicate_form = result.xform
        self.assertTrue(duplicate_form.is_duplicate)

        case = CommCareCase.objects.get_case(case_id, self.domain)
        self.assertIsNotNone(case)

        form = self.formdb.get_form(form_id)
        self.assertTrue(form.is_normal)
        self.assertIsNone(getattr(form, 'problem', None))
        self.assertEqual(duplicate_form.orig_id, form.form_id)


@sharded
class TestTransactionErrors(TransactionTestCase):
    domain = uuid.uuid4().hex

    def tearDown(self):
        FormProcessorTestUtils.delete_all_cases_forms_ledgers(self.domain)
        super().tearDown()

    def test_error_saving_case(self):
        form_id = uuid.uuid4().hex
        case_id = uuid.uuid4().hex

        error_on_save = patch.object(CommCareCase, 'save', side_effect=IntegrityError)
        with error_on_save, self.assertRaises(IntegrityError):
            submit_case_blocks(
                [CaseBlock(case_id=case_id, update={'a': "2"}).as_text()],
                self.domain,
                form_id=form_id
            )

        form = XFormInstance.objects.get_form(form_id)
        self.assertTrue(form.is_error)
        self.assertIsNotNone(form.get_xml())

    def test_error_saving_case_during_edit(self):
        form_id = uuid.uuid4().hex
        case_id = uuid.uuid4().hex
        submit_case_blocks(
            [CaseBlock(case_id=case_id, update={'a': "1"}).as_text()],
            self.domain,
            form_id=form_id
        )

        error_on_save = patch.object(CommCareCase, 'save', side_effect=IntegrityError)
        with error_on_save, self.assertRaises(IntegrityError):
            submit_case_blocks(
                [CaseBlock(case_id=case_id, update={'a': "2"}).as_text()],
                self.domain,
                form_id=form_id
            )

        [error_form_id] = XFormInstance.objects.get_form_ids_in_domain(self.domain, 'XFormError')
        self.assertNotEqual(error_form_id, form_id)
        form = XFormInstance.objects.get_form(error_form_id)
        self.assertTrue(form.is_error)
        self.assertIsNotNone(form.get_xml())

    def test_error_reprocessing_ledgers_after_borked_save(self):
        from corehq.apps.commtrack.tests.util import get_single_balance_block
        form_id, case_id, product_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex

        # setup by creating the case
        submit_case_blocks([CaseBlock(case_id=case_id, create=True).as_text()], self.domain)

        # submit a form that updates the case and ledger
        submit_case_blocks(
            [
                CaseBlock(case_id=case_id, update={'a': "1"}).as_text(),
                get_single_balance_block(case_id, product_id, 100),
            ],
            self.domain,
            form_id=form_id
        )

        # simulate an error by deleting the form XML
        form = XFormInstance.objects.get_form(form_id)
        form.get_attachment_meta('form.xml').delete()

        # re-submit the form again
        submit_case_blocks(
            [
                CaseBlock(case_id=case_id, update={'a': "1"}).as_text(),
                get_single_balance_block(case_id, product_id, 100),
            ],
            self.domain,
            form_id=form_id
        )

        form = XFormInstance.objects.get_form(form_id)
        self.assertTrue(form.is_normal)


@contextlib.contextmanager
def _patch_save_to_raise_error(test_class):
    sql_patch = patch(
        'corehq.form_processor.backends.sql.processor.FormProcessorSQL.save_processed_models',
        side_effect=InternalError
    )
    with sql_patch, test_class.assertRaises(InternalError):
        yield
