import uuid
from django.utils.unittest.case import TestCase
from casexml.apps.case.mock import CaseBlock
from casexml.apps.case.models import CommCareCase
from casexml.apps.case.util import post_case_blocks
from casexml.apps.case.xml import V2
from corehq.apps.domain.shortcuts import create_domain
from dimagi.utils.parsing import json_format_datetime
from corehq.apps.groups.models import Group
from corehq.apps.hqcase.exceptions import CaseAssignmentError
from corehq.apps.hqcase.utils import assign_case
from corehq.apps.users.models import CommCareUser
from corehq.apps.users.util import format_username

class CaseAssignmentTest(TestCase):
    domain = "case-assignment-test"
    original_owner = 'original-owner'

    def setUp(self):
        create_domain(self.domain)
        self.primary_user = CommCareUser.create(self.domain, format_username('case-assignment-user', self.domain), "****")

    def tearDown(self):
        self.primary_user.delete()

    def test_assign_to_unknown_user(self):
        case = self._new_case()
        try:
            assign_case(case, 'noonehere')
            self.fail('reassigning to nonexistent user should fail')
        except CaseAssignmentError:
            pass

    def test_assign_to_user_in_different_domain(self):
        other_user = CommCareUser.create('bad-domain', format_username('bad-domain-user', self.domain), "****")
        case = self._new_case()
        try:
            assign_case(case, other_user._id)
            self.fail('reassigning to user in wrong domain should fail')
        except CaseAssignmentError:
            pass

    def test_assign_no_related(self):
        self._make_tree()
        assign_case(self.primary, self.primary_user._id, include_subcases=False, include_parent_cases=False)
        self._check_state(new_owner_id=self.primary_user._id, expected_changed=[self.primary])

    def test_assign_subcases(self):
        self._make_tree()
        assign_case(self.primary, self.primary_user._id, include_subcases=True, include_parent_cases=False)
        self._check_state(new_owner_id=self.primary_user._id,
                          expected_changed=[self.primary, self.son, self.daughter,
                                            self.grandson, self.granddaughter, self.grandson2])

    def test_assign_parent_cases(self):
        self._make_tree()
        assign_case(self.primary, self.primary_user._id, include_subcases=False, include_parent_cases=True)
        self._check_state(new_owner_id=self.primary_user._id,
                          expected_changed=[self.grandfather, self.grandmother, self.parent, self.primary])

    def test_assign_all_related(self):
        self._make_tree()
        assign_case(self.primary, self.primary_user._id, include_subcases=True, include_parent_cases=True)
        self._check_state(new_owner_id=self.primary_user._id,
                          expected_changed=self.all)

    def test_assign_to_group(self):
        group = Group(users=[], name='case-assignment-group', domain=self.domain)
        group.save()
        self._make_tree()
        assign_case(self.primary, group._id, include_subcases=True, include_parent_cases=True)
        self._check_state(new_owner_id=group._id, expected_changed=self.all)

    def _make_tree(self):
        # create a tree that looks like this:
        #      grandmother    grandfather
        #                parent
        #                primary
        #         son               daughter
        # grandson  granddaughter   grandson2
        self.grandmother = self._new_case()
        self.grandfather = self._new_case()
        self.parent = self._new_case(index={'mom': ('person', self.grandmother._id),
                                            'dad': ('person', self.grandfather._id)})
        self.primary = self._new_case(index={'parent': ('person', self.parent._id)})
        self.son = self._new_case(index={'parent': ('person', self.primary._id)})
        self.daughter = self._new_case(index={'parent': ('person', self.primary._id)})
        self.grandson = self._new_case(index={'parent': ('person', self.son._id)})
        self.granddaughter = self._new_case(index={'parent': ('person', self.son._id)})
        self.grandson2 = self._new_case(index={'parent': ('person', self.daughter._id)})

        self.all = [self.grandmother, self.grandfather, self.parent, self.primary,
                    self.son, self.daughter, self.grandson, self.granddaughter, self.grandson2]
        for case in self.all:
            self.assertEqual(case.owner_id, self.original_owner)

    def _check_state(self, new_owner_id, expected_changed):
        expected_ids = set(c._id for c in expected_changed)
        for case in expected_changed:
            expected = CommCareCase.get(case._id)
            self.assertEqual(new_owner_id, expected.owner_id)
        for case in (c for c in self.all if c._id not in expected_ids):
            remaining = CommCareCase.get(case._id)
            self.assertEqual(self.original_owner, remaining.owner_id)

    def _new_case(self, index=None):
        index = index or {}
        id = uuid.uuid4().hex
        case_block = CaseBlock(
            create=True,
            case_id=id,
            case_type='person',
            owner_id=self.original_owner,
            version=V2,
            index=index,
        ).as_xml(format_datetime=json_format_datetime)
        post_case_blocks([case_block], {'domain': self.domain})
        return CommCareCase.get(id)
