from collections import namedtuple
from datetime import datetime

from django.db import models
from django.utils.translation import gettext as _

from dimagi.utils.couch import CriticalSection
from dimagi.utils.parsing import ISO_DATE_FORMAT

from corehq.apps.case_importer import exceptions

property_type = namedtuple('property_type', 'slug display')


class PROPERTY_TYPES:
    DATE = property_type('date', _('Date'))
    PLAIN = property_type('plain', _('Plain'))
    NUMBER = property_type('number', _('Number'))
    SELECT = property_type('select', _('Multiple Choice'))
    BARCODE = property_type('barcode', _('Barcode'))
    GPS = property_type('gps', _('GPS'))
    PHONE_NUMBER = property_type('phone_number', _('Phone Number'))
    PASSWORD = property_type('password', _('Password'))
    UNDEFINED = property_type('', _('No Type Currently Selected'))

    @classmethod
    def get_all(cls):
        return [t for t in cls.__dict__.values() if isinstance(t, property_type)]


class CaseType(models.Model):
    domain = models.CharField(max_length=255, default=None)
    name = models.CharField(max_length=255, default=None)
    description = models.TextField(default='', blank=True)
    fully_generated = models.BooleanField(default=False)

    class Meta(object):
        unique_together = ('domain', 'name')

    def __str__(self):
        return self.name or super().__str__()

    @classmethod
    def get_or_create(cls, domain, case_type):
        key = 'data-dict-case-type-{domain}-{type}'.format(
            domain=domain, type=case_type
        )
        with CriticalSection([key]):
            try:
                case_type_obj = CaseType.objects.get(domain=domain, name=case_type)
            except CaseType.DoesNotExist:
                case_type_obj = CaseType.objects.create(domain=domain, name=case_type)
            return case_type_obj

    def save(self, *args, **kwargs):
        from .util import get_data_dict_case_types
        get_data_dict_case_types.clear(self.domain)
        return super(CaseType, self).save(*args, **kwargs)


class CaseProperty(models.Model):
    case_type = models.ForeignKey(
        CaseType,
        on_delete=models.CASCADE,
        related_name='properties',
        related_query_name='property'
    )
    name = models.CharField(max_length=255, default=None)
    description = models.TextField(default='', blank=True)
    deprecated = models.BooleanField(default=False)
    data_type = models.CharField(
        choices=[(t.slug, t.display) for t in PROPERTY_TYPES.get_all()],
        max_length=20,
        default='',
        blank=True
    )
    group = models.TextField(default='', blank=True)

    class Meta(object):
        unique_together = ('case_type', 'name')

    def __str__(self):
        if self.name and self.case_type.name:
            return f'{self.case_type.name}.{self.name}'
        return super().__str__()

    @classmethod
    def get_or_create(cls, name, case_type, domain):
        key = 'data-dict-property-{domain}-{type}-{name}'.format(
            domain=domain, type=case_type, name=name
        )
        with CriticalSection([key]):
            try:
                prop = CaseProperty.objects.get(
                    name=name, case_type__name=case_type, case_type__domain=domain
                )
            except CaseProperty.DoesNotExist:
                case_type_obj = CaseType.get_or_create(domain, case_type)
                prop = CaseProperty.objects.create(case_type=case_type_obj, name=name)
            return prop

    def save(self, *args, **kwargs):
        from .util import get_data_dict_props_by_case_type, get_gps_properties
        get_data_dict_props_by_case_type.clear(self.case_type.domain)
        get_gps_properties.clear(self.case_type.domain, self.case_type.name)
        return super(CaseProperty, self).save(*args, **kwargs)

    def check_validity(self, value):
        if value and self.data_type == 'date':
            try:
                datetime.strptime(value, ISO_DATE_FORMAT)
            except ValueError:
                raise exceptions.InvalidDate(sample=value)
        elif value and self.data_type == 'select' and self.allowed_values.exists():
            if not self.allowed_values.filter(allowed_value=value).exists():
                raise exceptions.InvalidSelectValue(sample=value, message=self.valid_values_message)

    @property
    def valid_values_message(self):
        allowed_values = self.allowed_values.values_list('allowed_value', flat=True)
        allowed_string = ', '.join(f'"{av}"' for av in allowed_values)
        return _("Valid values: %s") % allowed_string


class CasePropertyAllowedValue(models.Model):
    case_property = models.ForeignKey(
        CaseProperty,
        on_delete=models.CASCADE,
        related_name='allowed_values',
        related_query_name='allowed_value'
    )
    allowed_value = models.CharField(max_length=255, blank=True, default='')
    description = models.TextField(default='', blank=True)

    class Meta(object):
        unique_together = ('case_property', 'allowed_value')

    def __str__(self):
        return f'{self.case_property} valid value: "{self.allowed_value}"'
