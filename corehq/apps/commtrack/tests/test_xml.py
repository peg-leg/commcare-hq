import os
import random
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Q
from django.test import TestCase
from django.test.utils import override_settings

from lxml import etree

from casexml.apps.case.mock import CaseBlock
from casexml.apps.case.tests.util import (
    check_xml_line_by_line,
    deprecated_check_user_has_case,
)
from casexml.apps.case.xml import V2
from casexml.apps.phone.restore import RestoreConfig, RestoreParams
from casexml.apps.phone.tests.utils import (
    deprecated_synclog_id_from_restore_payload,
)
from dimagi.utils.parsing import json_format_date, json_format_datetime

from corehq.apps.commtrack import const
from corehq.apps.commtrack.const import DAYS_IN_MONTH
from corehq.apps.commtrack.models import (
    CommtrackConfig,
    ConsumptionConfig,
    StockRestoreConfig,
)
from corehq.apps.commtrack.tests import util
from corehq.apps.commtrack.tests.data.balances import (
    SohReport,
    balance_enumerated,
    balance_first,
    balance_ota_block,
    balance_submission,
    products_xml,
    receipts_enumerated,
    submission_wrap,
    transfer_both,
    transfer_dest_only,
    transfer_first,
    transfer_source_only,
)
from corehq.apps.commtrack.tests.util import make_loc
from corehq.apps.consumption.shortcuts import (
    set_default_monthly_consumption_for_domain,
)
from corehq.apps.domain.models import Domain
from corehq.apps.groups.models import Group
from corehq.apps.hqcase.utils import submit_case_blocks
from corehq.apps.products.models import Product
from corehq.apps.receiverwrapper.util import submit_form_locally
from corehq.apps.users.dbaccessors import delete_all_users
from corehq.form_processor.backends.sql.dbaccessors import LedgerAccessorSQL
from corehq.form_processor.interfaces.dbaccessors import LedgerAccessors
from corehq.form_processor.models import CommCareCase, LedgerTransaction, XFormInstance
from corehq.form_processor.tests.utils import sharded
from corehq.sql_db.util import paginate_query_across_partitioned_databases
from testapps.test_pillowtop.utils import process_pillow_changes


@sharded
class XMLTest(TestCase):
    user_definitions = [util.FIXED_USER]

    def setUp(self):
        super(XMLTest, self).setUp()
        self.domain = util.bootstrap_domain(util.TEST_DOMAIN)
        util.bootstrap_location_types(self.domain.name)
        util.bootstrap_products(self.domain.name)
        self.products = sorted(Product.by_domain(self.domain.name), key=lambda p: p._id)
        self.ct_settings = CommtrackConfig.for_domain(self.domain.name)
        self.ct_settings.consumptionconfig = ConsumptionConfig(
            min_transactions=0,
            min_window=0,
            optimal_window=60,
        )
        self.ct_settings.save()
        self.ct_settings.consumptionconfig.commtrack_settings = self.ct_settings
        self.ct_settings.consumptionconfig.save()
        self.domain = Domain.get(self.domain._id)

        self.loc = make_loc('loc1')
        self.sp = self.loc.linked_supply_point()
        self.users = [util.bootstrap_user(self, **user_def) for user_def in self.user_definitions]
        self.user = self.users[0]
        self.case_ids = [self.sp.case_id]
        self.addCleanup(self.delete_ledger_values, self.sp.case_id)

    def delete_ledger_transactions(self, form_id):
        LedgerAccessorSQL.delete_ledger_transactions_for_form(self.case_ids, form_id)

    def delete_ledger_values(self, case_id):
        LedgerAccessorSQL.delete_ledger_values(case_id)

    def tearDown(self):
        delete_all_users()
        self.domain.delete()
        super(XMLTest, self).tearDown()


class CommTrackOTATest(XMLTest):

    def test_ota_blank_balances(self):
        self.assertFalse(util.get_ota_balance_xml(self.domain, self.user))

    def test_ota_basic(self):
        amounts = [
            SohReport(section_id='stock', product_id=p._id, amount=i * 10)
            for i, p in enumerate(self.products)
        ]
        form_id, report_date = _report_soh(amounts, self.sp.case_id, self.domain.name)
        self.addCleanup(self.delete_ledger_transactions, form_id)
        check_xml_line_by_line(
            self,
            balance_ota_block(
                self.sp,
                'stock',
                amounts,
                datestring=report_date,
            ),
            util.get_ota_balance_xml(self.domain, self.user)[0],
        )

    def test_ota_multiple_stocks(self):
        section_ids = sorted(('stock', 'losses', 'consumption'))
        amounts = [
            SohReport(section_id=section_id, product_id=p._id, amount=i * 10)
            for section_id in section_ids
            for i, p in enumerate(self.products)
        ]
        form_id, report_date = _report_soh(amounts, self.sp.case_id, self.domain.name)
        self.addCleanup(self.delete_ledger_transactions, form_id)
        balance_blocks = util.get_ota_balance_xml(self.domain, self.user)
        self.assertEqual(3, len(balance_blocks))
        for i, section_id in enumerate(section_ids):
            reports = [
                report for report in amounts if report.section_id == section_id
            ]
            check_xml_line_by_line(
                self,
                balance_ota_block(
                    self.sp,
                    section_id,
                    reports,
                    datestring=report_date,
                ),
                balance_blocks[i],
            )

    def test_ota_consumption(self):
        self.ct_settings.sync_consumption_fixtures = True
        self.ct_settings.consumptionconfig = ConsumptionConfig(
            min_transactions=0,
            min_window=0,
            optimal_window=60,
        )
        self.ct_settings.stockrestoreconfig = StockRestoreConfig(
            section_to_consumption_types={'stock': 'consumption'}
        )
        set_default_monthly_consumption_for_domain(self.domain.name, 5 * DAYS_IN_MONTH)
        self._save_settings_and_clear_cache()

        amounts = [
            SohReport(section_id='stock', product_id=p._id, amount=i * 10)
            for i, p in enumerate(self.products)
        ]
        form_id, report_date = _report_soh(amounts, self.sp.case_id, self.domain.name)
        self.addCleanup(self.delete_ledger_transactions, form_id)
        balance_blocks = _get_ota_balance_blocks(self.domain, self.user)
        self.assertEqual(2, len(balance_blocks))
        stock_block, consumption_block = balance_blocks
        check_xml_line_by_line(
            self,
            balance_ota_block(
                self.sp,
                'stock',
                amounts,
                datestring=report_date,
            ),
            stock_block,
        )
        check_xml_line_by_line(
            self,
            balance_ota_block(
                self.sp,
                'consumption',
                [SohReport(section_id='', product_id=p._id, amount=150) for p in self.products],
                datestring=report_date,
            ),
            consumption_block,
        )

    def test_force_consumption(self):
        self.ct_settings.sync_consumption_fixtures = True
        self.ct_settings.consumptionconfig = ConsumptionConfig(
            min_transactions=0,
            min_window=0,
            optimal_window=60,
        )
        self.ct_settings.stockrestoreconfig = StockRestoreConfig(
            section_to_consumption_types={'stock': 'consumption'},
        )
        set_default_monthly_consumption_for_domain(self.domain.name, 5)
        self._save_settings_and_clear_cache()

        balance_blocks = _get_ota_balance_blocks(self.domain, self.user)
        self.assertEqual(0, len(balance_blocks))

        self.ct_settings.stockrestoreconfig.force_consumption_case_types = [const.SUPPLY_POINT_CASE_TYPE]
        self._save_settings_and_clear_cache()

        balance_blocks = _get_ota_balance_blocks(self.domain, self.user)
        # with no data, there should be no consumption block
        self.assertEqual(0, len(balance_blocks))

        self.ct_settings.stockrestoreconfig.use_dynamic_product_list = True
        self._save_settings_and_clear_cache()

        balance_blocks = _get_ota_balance_blocks(self.domain, self.user)
        self.assertEqual(1, len(balance_blocks))
        [balance_block] = balance_blocks
        element = etree.fromstring(balance_block)
        self.assertEqual(3, len([child for child in element]))

    def _save_settings_and_clear_cache(self):
        # since the commtrack settings object is stored as a memoized property on the domain
        # we need to refresh that as well
        self.ct_settings.save()
        self.ct_settings.stockrestoreconfig.save()
        self.domain = Domain.get(self.domain._id)


class CommTrackSubmissionTest(XMLTest):

    @classmethod
    def setUpClass(cls):
        super(CommTrackSubmissionTest, cls).setUpClass()
        cls.process_legder_changes = process_pillow_changes('LedgerToElasticsearchPillow')

    def setUp(self):
        super(CommTrackSubmissionTest, self).setUp()
        loc2 = make_loc('loc2')
        self.sp2 = loc2.linked_supply_point()
        self.case_ids.append(self.sp2.case_id)
        self.addCleanup(self.delete_ledger_values, self.sp2.case_id)

    @override_settings(CASEXML_FORCE_DOMAIN_CHECK=False)
    def submit_xml_form(self, xml_method, timestamp=None, date_formatter=json_format_datetime,
                        device_id='351746051189879', **submit_extras):
        instance_id = uuid.uuid4().hex
        instance = submission_wrap(
            instance_id,
            self.products,
            self.user,
            self.sp.case_id,
            self.sp2.case_id,
            xml_method,
            timestamp=timestamp,
            date_formatter=date_formatter,
            device_id=device_id,
        )
        with self.process_legder_changes:
            submit_form_locally(
                instance=instance,
                domain=self.domain.name,
                **submit_extras
            )
        self.addCleanup(self.delete_ledger_transactions, instance_id)
        return instance_id

    def check_product_stock(self, case, product_id, expected_soh, expected_qty, section_id='stock'):
        if not isinstance(expected_qty, Decimal):
            expected_qty = Decimal(str(expected_qty))
        if not isinstance(expected_soh, Decimal):
            expected_soh = Decimal(str(expected_soh))

        latest_trans = LedgerAccessors(self.domain.name).get_latest_transaction(
            case.case_id, section_id, product_id
        )
        self.assertIsNotNone(latest_trans)
        self.assertEqual(section_id, latest_trans.section_id)
        self.assertEqual(expected_soh, latest_trans.stock_on_hand)

        if latest_trans.type == LedgerTransaction.TYPE_TRANSFER:
            self.assertEqual(int(expected_qty), latest_trans.delta)

    def _get_all_ledger_transactions(self, q_):
        return list(paginate_query_across_partitioned_databases(LedgerTransaction, q_))


class CommTrackBalanceTransferTest(CommTrackSubmissionTest):

    def test_balance_submit(self):
        amounts = [(p._id, float(i * 10)) for i, p in enumerate(self.products)]
        self.submit_xml_form(balance_submission(amounts))
        for product, amt in amounts:
            self.check_product_stock(self.sp, product, amt, 0)

    def test_balance_submit_date(self):
        amounts = [(p._id, float(i * 10)) for i, p in enumerate(self.products)]
        self.submit_xml_form(balance_submission(amounts), date_formatter=json_format_date)
        for product, amt in amounts:
            self.check_product_stock(self.sp, product, amt, 0)

    def test_balance_enumerated(self):
        amounts = [(p._id, float(i * 10)) for i, p in enumerate(self.products)]
        self.submit_xml_form(balance_enumerated(amounts))
        for product, amt in amounts:
            self.check_product_stock(self.sp, product, amt, 0)

    def test_balance_consumption(self):
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts))

        final_amounts = [(p._id, float(50 - 10 * i)) for i, p in enumerate(self.products)]
        self.submit_xml_form(balance_submission(final_amounts))
        for product, amt in final_amounts:
            self.check_product_stock(self.sp, product, amt, 0)
            inferred = amt - initial
            sql_txn = LedgerAccessors(self.domain.name).get_latest_transaction(
                self.sp.case_id, 'stock', product
            )
            self.assertEqual(inferred, sql_txn.delta)

    def test_balance_consumption_with_date(self):
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts), date_formatter=json_format_date)

        final_amounts = [(p._id, float(50 - 10 * i)) for i, p in enumerate(self.products)]
        self.submit_xml_form(balance_submission(final_amounts), date_formatter=json_format_date)
        for product, amt in final_amounts:
            self.check_product_stock(self.sp, product, amt, 0)

    def test_archived_product_submissions(self):
        """
        This is basically the same as above, but separated to be
        verbose about what we are checking (and to make it easy
        to change the expected behavior if the requirements change
        soon.
        """
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        final_amounts = [(p._id, float(50 - 10 * i)) for i, p in enumerate(self.products)]

        self.submit_xml_form(balance_submission(initial_amounts))
        self.products[1].archive()
        self.submit_xml_form(balance_submission(final_amounts))

        for product, amt in final_amounts:
            self.check_product_stock(self.sp, product, amt, 0)

    def test_balance_submit_multiple_stocks(self):
        def _random_amounts():
            return [(p._id, float(random.randint(0, 100))) for i, p in enumerate(self.products)]

        section_ids = ('stock', 'losses', 'consumption')
        stock_amounts = [(id, _random_amounts()) for id in section_ids]
        for section_id, amounts in stock_amounts:
            self.submit_xml_form(balance_submission(amounts, section_id=section_id))

        for section_id, amounts in stock_amounts:
            for product, amt in amounts:
                self.check_product_stock(self.sp, product, amt, 0, section_id)

    def test_transfer_dest_only(self):
        amounts = [(p._id, float(i * 10)) for i, p in enumerate(self.products)]
        self.submit_xml_form(transfer_dest_only(amounts))
        for product, amt in amounts:
            self.check_product_stock(self.sp, product, amt, amt)

    def test_transfer_source_only(self):
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts))

        deductions = [(p._id, float(50 - 10 * i)) for i, p in enumerate(self.products)]
        self.submit_xml_form(transfer_source_only(deductions))
        for product, amt in deductions:
            self.check_product_stock(self.sp, product, initial - amt, -amt)

    def test_transfer_both(self):
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts))

        transfers = [(p._id, float(50 - 10 * i)) for i, p in enumerate(self.products)]
        self.submit_xml_form(transfer_both(transfers))
        for product, amt in transfers:
            self.check_product_stock(self.sp, product, initial - amt, -amt)
            self.check_product_stock(self.sp2, product, amt, amt)

    def test_transfer_with_date(self):
        amounts = [(p._id, float(i * 10)) for i, p in enumerate(self.products)]
        self.submit_xml_form(transfer_dest_only(amounts), date_formatter=json_format_date)
        for product, amt in amounts:
            self.check_product_stock(self.sp, product, amt, amt)

    def test_transfer_enumerated(self):
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts))

        receipts = [(p._id, float(50 - 10 * i)) for i, p in enumerate(self.products)]
        self.submit_xml_form(receipts_enumerated(receipts))
        for product, amt in receipts:
            self.check_product_stock(self.sp, product, initial + amt, amt)

    def test_balance_first_doc_order(self):
        initial = float(100)
        balance_amounts = [(p._id, initial) for p in self.products]
        transfers = [(p._id, float(50 - 10 * i)) for i, p in enumerate(self.products)]
        self.submit_xml_form(balance_first(balance_amounts, transfers))
        for product, amt in transfers:
            self.check_product_stock(self.sp, product, initial + amt, amt)

    def test_transfer_first_doc_order(self):
        # first set to 100
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts))

        # then mark some receipts
        transfers = [(p._id, float(50 - 10 * i + 3)) for i, p in enumerate(self.products)]
        # then set to 50
        final = float(50)
        balance_amounts = [(p._id, final) for p in self.products]
        self.submit_xml_form(transfer_first(transfers, balance_amounts))
        for product, amt in transfers:
            self.check_product_stock(self.sp, product, final, 0)

    def test_blank_quantities(self):
        # submitting a bunch of blank data shouldn't submit transactions
        # so lets submit some initial data and make sure we don't modify it
        # or have new transactions
        initial = float(100)
        initial_amounts = [(p._id, initial) for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts))

        trans_count = len(self._get_all_ledger_transactions(Q()))

        initial_amounts = [(p._id, '') for p in self.products]
        self.submit_xml_form(balance_submission(initial_amounts))

        self.assertEqual(trans_count, len(self._get_all_ledger_transactions(Q())))
        for product in self.products:
            self.check_product_stock(self.sp, product._id, 100, 0)

    def test_blank_product_id(self):
        initial = float(100)
        balances = [('', initial)]
        instance_id = self.submit_xml_form(balance_submission(balances))
        instance = XFormInstance.objects.get_form(instance_id, self.domain.name)
        self.assertTrue(instance.is_error)
        self.assertTrue('MissingProductId' in instance.problem)

    def test_blank_case_id_in_balance(self):
        form = submit_case_blocks(
            case_blocks=util.get_single_balance_block(
                case_id='',
                product_id=self.products[0]._id,
                quantity=100
            ),
            domain=self.domain.name,
        )[0]
        self.addCleanup(self.delete_ledger_transactions, form.form_id)
        instance = XFormInstance.objects.get_form(form.form_id, self.domain.name)
        self.assertTrue(instance.is_error)
        self.assertTrue('IllegalCaseId' in instance.problem)

    def test_blank_case_id_in_transfer(self):
        form = submit_case_blocks(
            case_blocks=util.get_single_transfer_block(
                src_id='', dest_id='', product_id=self.products[0]._id, quantity=100,
            ),
            domain=self.domain.name,
        )[0]
        self.addCleanup(self.delete_ledger_transactions, form.form_id)
        instance = XFormInstance.objects.get_form(form.form_id, self.domain.name)
        self.assertTrue(instance.is_error)
        self.assertTrue('IllegalCaseId' in instance.problem)


class BugSubmissionsTest(CommTrackSubmissionTest):

    def test_device_report_submissions_ignored(self):
        """
        submit a device report with a stock block and make sure it doesn't
        get processed
        """
        def _assert_no_stock_transactions():
            self.assertEqual(0, len(self._get_all_ledger_transactions(Q(form_id=form_id))))

        form_id = uuid.uuid4().hex
        _assert_no_stock_transactions()

        fpath = os.path.join(os.path.dirname(__file__), 'data', 'xml', 'device_log.xml')
        with open(fpath) as f:
            form = f.read()
        amounts = [(p._id, 10) for p in self.products]
        product_block = products_xml(amounts)
        form = form.format(
            form_id=form_id,
            user_id=self.user._id,
            date=json_format_datetime(datetime.utcnow()),
            sp_id=self.sp.case_id,
            product_block=product_block
        )
        submit_form_locally(
            instance=form,
            domain=self.domain.name,
        )

        _assert_no_stock_transactions()

    def test_xform_id_added_to_case_xform_list(self):
        initial_amounts = [(p._id, float(100)) for p in self.products]
        submissions = [balance_submission([amount]) for amount in initial_amounts]
        instance_id = self.submit_xml_form(
            ''.join(submissions),
            timestamp=datetime.utcnow() + timedelta(-30)
        )

        case = CommCareCase.objects.get_case(self.sp.case_id, self.domain.name)
        self.assertIn(instance_id, case.xform_ids)

    def test_xform_id_added_to_case_xform_list_only_once(self):
        initial_amounts = [(p._id, float(100)) for p in self.products]
        submissions = [balance_submission([amount]) for amount in initial_amounts]
        case_block = CaseBlock(
            create=False,
            case_id=self.sp.case_id,
            user_id='jack',
            update={'test': '1'}
        ).as_text()
        instance_id = self.submit_xml_form(
            ''.join([case_block] + submissions),
            timestamp=datetime.utcnow() + timedelta(-30)
        )

        case = CommCareCase.objects.get_case(self.sp.case_id, self.domain.name)
        self.assertIn(instance_id, case.xform_ids)
        # make sure the ID only got added once
        self.assertEqual(len(case.xform_ids), len(set(case.xform_ids)))

    def test_archived_form_gets_removed_from_case_xform_ids(self):
        initial_amounts = [(p._id, float(100)) for p in self.products]
        instance_id = self.submit_xml_form(
            balance_submission(initial_amounts),
            timestamp=datetime.utcnow() + timedelta(-30)
        )

        case = CommCareCase.objects.get_case(self.sp.case_id, self.domain.name)
        self.assertIn(instance_id, case.xform_ids)

        form = XFormInstance.objects.get_form(instance_id, self.domain.name)
        form.archive()

        case = CommCareCase.objects.get_case(self.sp.case_id, self.domain.name)
        self.assertNotIn(instance_id, case.xform_ids)


class CommTrackSyncTest(CommTrackSubmissionTest):

    def setUp(self):
        super(CommTrackSyncTest, self).setUp()
        self.group = Group(domain=util.TEST_DOMAIN, name='commtrack-folks',
                           users=[self.user._id], case_sharing=True)
        self.group._id = self.sp.owner_id
        self.group.save()

        self.restore_user = self.user.to_ota_restore_user()
        self.sp_block = CaseBlock(
            case_id=self.sp.case_id,
        ).as_xml()

        # get initial restore token
        restore_config = RestoreConfig(
            project=self.domain,
            restore_user=self.restore_user,
            params=RestoreParams(version=V2),
        )
        self.sync_log_id = deprecated_synclog_id_from_restore_payload(
            restore_config.get_payload().as_string())

    def testStockSyncToken(self):
        # first restore should not have the updated case
        deprecated_check_user_has_case(
            self, self.restore_user, self.sp_block, should_have=False,
            restore_id=self.sync_log_id, version=V2)

        # submit with token
        amounts = [(p._id, float(i * 10)) for i, p in enumerate(self.products)]
        self.submit_xml_form(balance_submission(amounts), last_sync_token=self.sync_log_id,
                             device_id=None)
        # now restore should have the case
        deprecated_check_user_has_case(
            self, self.restore_user, self.sp_block, should_have=True,
            restore_id=self.sync_log_id, version=V2, line_by_line=False)


class CommTrackArchiveSubmissionTest(CommTrackSubmissionTest):

    def setUp(self):
        super(CommTrackArchiveSubmissionTest, self).setUp()
        self.ct_settings.use_auto_consumption = True
        self.ct_settings.save()

    def test_archive_last_form(self):
        initial_amounts = [(p._id, float(100)) for p in self.products]
        self.submit_xml_form(
            balance_submission(initial_amounts),
            timestamp=datetime.utcnow() + timedelta(-30)
        )

        final_amounts = [(p._id, float(50)) for i, p in enumerate(self.products)]
        second_form_id = self.submit_xml_form(balance_submission(final_amounts))

        ledger_accessors = LedgerAccessors(self.domain.name)

        def _assert_initial_state():
            self.assertEqual(3, len(self._get_all_ledger_transactions(Q(form_id=second_form_id))))

            ledger_values = ledger_accessors.get_ledger_values_for_case(self.sp.case_id)
            self.assertEqual(3, len(ledger_values))
            for lv in ledger_values:
                self.assertEqual(50, lv.stock_on_hand)
                self.assertEqual(
                    round(float(lv.daily_consumption), 2),
                    1.67
                )

        # check initial setup
        _assert_initial_state()

        # archive and confirm commtrack data is deleted
        form = XFormInstance.objects.get_form(second_form_id, self.domain.name)
        with self.process_legder_changes:
            form.archive()

        self.assertEqual(0, len(self._get_all_ledger_transactions(Q(form_id=second_form_id))))

        ledger_values = ledger_accessors.get_ledger_values_for_case(self.sp.case_id)
        self.assertEqual(3, len(ledger_values))
        for state in ledger_values:
            # balance should be reverted to 100 in the ledger value
            self.assertEqual(100, int(state.stock_on_hand))
            # consumption should be none since there will only be 1 data point
            self.assertIsNone(state.daily_consumption)

        # unarchive and confirm commtrack data is restored
        with self.process_legder_changes:
            form.unarchive()
        _assert_initial_state()

    def test_archive_only_form(self):
        # check no data in stock states
        ledger_accessors = LedgerAccessors(self.domain.name)
        ledger_values = ledger_accessors.get_ledger_values_for_case(self.sp.case_id)
        self.assertEqual(0, len(ledger_values))

        initial_amounts = [(p._id, float(100)) for p in self.products]
        form_id = self.submit_xml_form(balance_submission(initial_amounts))

        # check that we made stuff
        def _assert_initial_state():
            self.assertEqual(3, len(self._get_all_ledger_transactions(Q(form_id=form_id))))

            ledger_values = ledger_accessors.get_ledger_values_for_case(self.sp.case_id)
            self.assertEqual(3, len(ledger_values))
            for state in ledger_values:
                self.assertEqual(100, int(state.stock_on_hand))

        _assert_initial_state()

        # archive and confirm commtrack data is cleared
        form = XFormInstance.objects.get_form(form_id, self.domain.name)
        form.archive()
        self.assertEqual(0, len(ledger_accessors.get_ledger_values_for_case(self.sp.case_id)))
        self.assertEqual(0, len(self._get_all_ledger_transactions(Q(form_id=form_id))))

        # unarchive and confirm commtrack data is restored
        form.unarchive()
        _assert_initial_state()


def _report_soh(soh_reports, case_id, domain):
    report_date = json_format_datetime(datetime.utcnow())
    balance_blocks = [
        util.get_single_balance_block(
            case_id,
            report.product_id,
            report.amount,
            report_date,
            section_id=report.section_id
        )
        for report in soh_reports
    ]
    form = submit_case_blocks(balance_blocks, domain)[0]
    received_on = json_format_datetime(XFormInstance.objects.get_form(form.form_id, domain).received_on)
    return form.form_id, received_on


def _get_ota_balance_blocks(project, user):
    restore_config = RestoreConfig(
        project=project,
        restore_user=user.to_ota_restore_user(),
        params=RestoreParams(version=V2),
    )
    return util.extract_balance_xml(restore_config.get_payload().as_string())
