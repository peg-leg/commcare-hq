import html
import re
from collections import defaultdict

from django.utils.translation import gettext as _

from memoized import memoized

from corehq import toggles
from corehq.apps.app_manager import id_strings
from corehq.apps.app_manager.exceptions import (
    DuplicateInstanceIdError,
    UnknownInstanceError,
)
from corehq.apps.app_manager.suite_xml.contributors import PostProcessor
from corehq.apps.app_manager.suite_xml.xml_models import Instance
from corehq.apps.app_manager.util import (
    module_offers_search,
    module_uses_inline_search,
)
from corehq.util.timer import time_method


class EntryInstances(PostProcessor):
    """Adds instance declarations to the suite file

    See docs/apps/instances.rst"""

    IGNORED_INSTANCES = {
        'jr://instance/remote',
        'jr://instance/search-input',
    }

    @time_method()
    def update_suite(self):
        for entry in self.suite.entries:
            self.add_entry_instances(entry)
        for remote_request in self.suite.remote_requests:
            self.add_entry_instances(remote_request)

    def add_entry_instances(self, entry):
        xpaths = self._get_all_xpaths_for_entry(entry)
        known_instances, unknown_instance_ids = get_all_instances_referenced_in_xpaths(self.app, xpaths)
        custom_instances, unknown_instance_ids = self._get_custom_instances(
            entry,
            known_instances,
            unknown_instance_ids
        )
        all_instances = known_instances | custom_instances
        self.require_instances(entry, instances=all_instances, instance_ids=unknown_instance_ids)

    def _get_all_xpaths_for_entry(self, entry):
        relevance_by_menu, menu_by_command = self._get_menu_relevance_mapping()
        details_by_id = self._get_detail_mapping()
        detail_ids = set()
        xpaths = set()

        for datum in entry.all_datums:
            detail_ids.add(datum.detail_confirm)
            detail_ids.add(datum.detail_select)
            detail_ids.add(datum.detail_inline)
            detail_ids.add(datum.detail_persistent)
            xpaths.add(datum.nodeset)
            xpaths.add(datum.function)
        for query in entry.queries:
            xpaths.update({data.ref for data in query.data})
            for prompt in query.prompts:
                if prompt.itemset:
                    xpaths.add(prompt.itemset.nodeset)
                if prompt.required:
                    xpaths.add(prompt.required)
                if prompt.default_value:
                    xpaths.add(prompt.default_value)
        if entry.post:
            if entry.post.relevant:
                xpaths.add(entry.post.relevant)
            for data in entry.post.data:
                xpaths.update(
                    xp for xp in [data.ref, data.nodeset, data.exclude] if xp
                )

        details = [details_by_id[detail_id] for detail_id in detail_ids if detail_id]

        entry_id = entry.command.id
        if entry_id in menu_by_command:
            menu_id = menu_by_command[entry_id]
            relevances = relevance_by_menu[menu_id]
            xpaths.update(relevances)

        for detail in details:
            xpaths.update(detail.get_all_xpaths())
        for assertion in getattr(entry, 'assertions', []):
            xpaths.add(assertion.test)
        if entry.stack:
            for frame in entry.stack.frames:
                xpaths.update(frame.get_xpaths())
        xpaths.discard(None)
        return xpaths

    @memoized
    def _get_detail_mapping(self):
        return {detail.id: detail for detail in self.suite.details}

    @memoized
    def _get_menu_relevance_mapping(self):
        relevance_by_menu = defaultdict(list)
        menu_by_command = {}
        for menu in self.suite.menus:
            for command in menu.commands:
                menu_by_command[command.id] = menu.id
                if command.relevant:
                    relevance_by_menu[menu.id].append(command.relevant)
            if menu.relevant:
                relevance_by_menu[menu.id].append(menu.relevant)

        return relevance_by_menu, menu_by_command

    def _get_custom_instances(self, entry, known_instances, required_instances):
        if entry.command.id not in self._form_module_by_command_id:
            return set(), required_instances

        known_instance_ids = {instance.id: instance for instance in known_instances}
        form, module = self._form_module_by_command_id[entry.command.id]
        custom_instances = []
        if hasattr(entry, 'form'):
            custom_instances.extend(
                Instance(id=instance.instance_id, src=instance.instance_path)
                for instance in form.custom_instances
            )
        if entry.queries:
            custom_instances.extend([
                Instance(id=prop.itemset.instance_id, src=prop.itemset.instance_uri)
                for prop in module.search_config.properties
                if prop.itemset.instance_id
            ])

        # sorted list to prevent intermittent test failures
        custom_instances = set(sorted(custom_instances, key=lambda i: i.id))

        for instance in list(custom_instances):
            existing = known_instance_ids.get(instance.id)
            if existing:
                if existing.src != instance.src:
                    raise DuplicateInstanceIdError(
                        _("Duplicate custom instance in {}: {}").format(entry.command.id, instance.id))

                # we already have this one, so we can ignore it
                custom_instances.discard(instance)

            # Remove custom instances from required instances, but add them even if they aren't referenced anywhere
            required_instances.discard(instance.id)
        return custom_instances, required_instances

    @property
    @memoized
    def _form_module_by_command_id(self):
        """Map the command ID to the form and module.

        Module must be included since ``form.get_module()`` does not return the correct
        module for ``ShadowModule`` forms
        """
        by_command = {}
        for module in self.app.get_modules():
            if module_offers_search(module) and not module_uses_inline_search(module):
                by_command[id_strings.search_command(module)] = (None, module)

            for form in module.get_suite_forms():
                by_command[id_strings.form_command(form, module)] = (form, module)
        return by_command

    @staticmethod
    def require_instances(entry, instances=(), instance_ids=()):
        used = {(instance.id, instance.src) for instance in entry.instances}
        instance_order_updated = EntryInstances.update_instance_order(entry)
        for instance in instances:
            if instance.src in EntryInstances.IGNORED_INSTANCES:
                continue
            if (instance.id, instance.src) not in used:
                entry.instances.append(
                    # it's important to make a copy,
                    # since these can't be reused
                    Instance(id=instance.id, src=instance.src)
                )
                if not instance_order_updated:
                    instance_order_updated = EntryInstances.update_instance_order(entry)
        covered_ids = {instance_id for instance_id, _ in used}
        for instance_id in instance_ids:
            if instance_id not in covered_ids:
                raise UnknownInstanceError(
                    "Instance reference not recognized: {} in XPath \"{}\""
                    # to get xpath context to show in this error message
                    # make instance_id a unicode subclass with an xpath property
                    .format(instance_id, getattr(instance_id, 'xpath', "(XPath Unknown)")))

        sorted_instances = sorted(entry.instances, key=lambda instance: instance.id)
        if sorted_instances != entry.instances:
            entry.instances = sorted_instances

    @staticmethod
    def update_instance_order(entry):
        """Make sure the first instance gets inserted right after the command.
        Once you "suggest" a placement to eulxml, it'll follow your lead and place
        the rest of them there too"""
        if entry.instances:
            instance_node = entry.node.find('instance')
            command_node = entry.node.find('command')
            entry.node.remove(instance_node)
            entry.node.insert(entry.node.index(command_node) + 1, instance_node)
            return True


_factory_map = {}


def get_instance_factory(instance_name):
    """Get the instance factory for an instance name (ID).
    This relies on a naming convention for instances: "scheme:id"

    See docs/apps/instances.rst"""
    try:
        scheme, _ = instance_name.split(':', 1)
    except ValueError:
        scheme = instance_name

    return _factory_map.get(scheme, null_factory)


def null_factory(app, instance_name):
    return None


class register_factory(object):

    def __init__(self, *schemes):
        self.schemes = schemes

    def __call__(self, fn):
        for scheme in self.schemes:
            _factory_map[scheme] = fn
        return fn


INSTANCE_KWARGS_BY_ID = {
    'groups': dict(id='groups', src='jr://fixture/user-groups'),
    'reports': dict(id='reports', src='jr://fixture/commcare:reports'),
    'ledgerdb': dict(id='ledgerdb', src='jr://instance/ledgerdb'),
    'casedb': dict(id='casedb', src='jr://instance/casedb'),
    'commcaresession': dict(id='commcaresession', src='jr://instance/session'),
    'registry': dict(id='registry', src='jr://instance/remote'),
    'selected_cases': dict(id='selected_cases', src='jr://instance/selected-entities'),
    'search_selected_cases': dict(id='search_selected_cases', src='jr://instance/selected-entities'),
}


@register_factory(*list(INSTANCE_KWARGS_BY_ID.keys()))
def preset_instances(app, instance_name):
    kwargs = INSTANCE_KWARGS_BY_ID[instance_name]
    return Instance(**kwargs)


@memoized
@register_factory('item-list', 'schedule', 'indicators', 'commtrack')
def generic_fixture_instances(app, instance_name):
    return Instance(id=instance_name, src='jr://fixture/{}'.format(instance_name))


@register_factory('search-input')
def search_input_instances(app, instance_name):
    return Instance(id=instance_name, src='jr://instance/search-input')


@register_factory('results')
def remote_instances(app, instance_name):
    return Instance(id=instance_name, src='jr://instance/remote')


@register_factory('commcare')
def commcare_fixture_instances(app, instance_name):
    if instance_name == 'commcare:reports' and toggles.MOBILE_UCR.enabled(app.domain):
        return Instance(id=instance_name, src='jr://fixture/{}'.format(instance_name))


def _commcare_reports_instances(app, instance_name, prefix):
    from corehq.apps.app_manager.suite_xml.features.mobile_ucr import (
        get_uuids_by_instance_id,
    )
    if instance_name.startswith(prefix) and toggles.MOBILE_UCR.enabled(app.domain):
        instance_id = instance_name[len(prefix):]
        uuid = get_uuids_by_instance_id(app).get(instance_id, [instance_id])[0]
        return Instance(id=instance_name, src='jr://fixture/{}{}'.format(prefix, uuid))


@register_factory('commcare-reports')
def commcare_reports_fixture_instances(app, instance_name):
    return _commcare_reports_instances(app, instance_name, 'commcare-reports:')


@register_factory('commcare-reports-filters')
def commcare_reports_filters_instances(app, instance_name):
    return _commcare_reports_instances(app, instance_name, 'commcare-reports-filters:')


@register_factory('locations')
def location_fixture_instances(app, instance_name):
    from corehq.apps.locations.models import LocationFixtureConfiguration
    if (toggles.HIERARCHICAL_LOCATION_FIXTURE.enabled(app.domain)
            and not LocationFixtureConfiguration.for_domain(app.domain).sync_flat_fixture):
        return Instance(id=instance_name, src='jr://fixture/commtrack:{}'.format(instance_name))
    return Instance(id=instance_name, src='jr://fixture/{}'.format(instance_name))


def get_all_instances_referenced_in_xpaths(app, xpaths):
    instances = set()
    unknown_instance_ids = set()
    for xpath in set(xpaths):
        if not xpath:
            continue

        instance_names = get_instance_names(xpath)
        for instance_name in instance_names:
            factory = get_instance_factory(instance_name)
            instance = factory(app, instance_name)
            if instance:
                instances.add(instance)
            else:
                class UnicodeWithContext(str):
                    pass
                instance_name = UnicodeWithContext(instance_name)
                instance_name.xpath = xpath
                unknown_instance_ids.add(instance_name)
    return instances, unknown_instance_ids


instance_re = re.compile(r"""instance\(['"]([\w\-:]+)['"]\)""", re.UNICODE)


def get_instance_names(xpath):
    unescaped = html.unescape(xpath)
    return set(re.findall(instance_re, unescaped))
