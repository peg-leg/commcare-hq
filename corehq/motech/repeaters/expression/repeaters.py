from django.utils.translation import gettext_lazy as _

from memoized import memoized

from dimagi.ext.couchdbkit import DictProperty, StringProperty

from corehq.apps.userreports.expressions.factory import ExpressionFactory
from corehq.apps.userreports.filters.factory import FilterFactory
from corehq.apps.userreports.specs import EvaluationContext, FactoryContext
from corehq.form_processor.models import CommCareCase
from corehq.motech.repeaters.expression.repeater_generators import (
    ExpressionPayloadGenerator,
)
from corehq.motech.repeaters.models import OptionValue, Repeater, SQLRepeater
from corehq.toggles import EXPRESSION_REPEATER


class BaseExpressionRepeater(Repeater):
    """Uses a UCR dict expression to send a generic json response
    """
    class Meta:
        app_label = 'repeaters'

    configured_filter = DictProperty()
    configured_expression = DictProperty()
    url_template = StringProperty()
    payload_generator_classes = (ExpressionPayloadGenerator,)

    @property
    @memoized
    def parsed_filter(self):
        return FilterFactory.from_spec(self.configured_filter, FactoryContext.empty())

    @property
    @memoized
    def parsed_expression(self):
        return ExpressionFactory.from_spec(self.configured_expression, FactoryContext.empty())

    @classmethod
    def available_for_domain(cls, domain):
        return EXPRESSION_REPEATER.enabled(domain)

    def allowed_to_forward(self, payload):
        payload_json = payload.to_json()
        return self.parsed_filter(payload_json, EvaluationContext(payload_json))

    @memoized
    def get_payload(self, repeat_record):
        return self.generator.get_payload(
            repeat_record,
            self.payload_doc(repeat_record),
            self.parsed_expression,
        )

    def get_url(self, repeat_record):
        base_url = super().get_url(repeat_record)
        if self.url_template:
            return base_url + self.generator.get_url(
                repeat_record,
                self.url_template,
                self.payload_doc(repeat_record),
            )
        return base_url

    @classmethod
    def _migration_get_sql_model_class(cls):
        return SQLBaseExpressionRepeater

    @classmethod
    def _migration_get_fields(cls):
        return super()._migration_get_fields() + ["configured_filter", "configured_expression"]


class CaseExpressionRepeater(BaseExpressionRepeater):
    friendly_name = _("Configurable Case Forwarder")

    @memoized
    def payload_doc(self, repeat_record):
        return CommCareCase.objects.get_case(repeat_record.payload_id, repeat_record.domain).to_json()

    @classmethod
    def _migration_get_sql_model_class(cls):
        return SQLCaseExpressionRepeater


class SQLBaseExpressionRepeater(SQLRepeater):
    """Uses a UCR dict expression to send a generic json response
    """
    class Meta:
        app_label = 'repeaters'
        proxy = True

    configured_filter = OptionValue(default=dict)
    configured_expression = OptionValue(default=dict)
    payload_generator_classes = (ExpressionPayloadGenerator,)

    @property
    @memoized
    def parsed_filter(self):
        return FilterFactory.from_spec(self.configured_filter, FactoryContext.empty())

    @property
    @memoized
    def parsed_expression(self):
        return ExpressionFactory.from_spec(self.configured_expression, FactoryContext.empty())

    @classmethod
    def available_for_domain(cls, domain):
        return EXPRESSION_REPEATER.enabled(domain)

    def allowed_to_forward(self, payload):
        payload_json = payload.to_json()
        return self.parsed_filter(payload_json, EvaluationContext(payload_json))

    @memoized
    def get_payload(self, repeat_record):
        return self.generator.get_payload(
            repeat_record,
            self.payload_doc(repeat_record),
            self.parsed_expression,
        )

    @classmethod
    def _migration_get_couch_model_class(cls):
        return BaseExpressionRepeater

    @classmethod
    def _migration_get_fields(cls):
        return super()._migration_get_fields() + ["configured_filter", "configured_expression"]


class SQLCaseExpressionRepeater(SQLBaseExpressionRepeater):

    friendly_name = _("Configurable Case Repeater")

    class Meta:
        app_label = 'repeaters'
        proxy = True

    @memoized
    def payload_doc(self, repeat_record):
        return CommCareCase.objects.get_case(repeat_record.payload_id, repeat_record.domain).to_json()

    @classmethod
    def _migration_get_couch_model_class(cls):
        return CaseExpressionRepeater
