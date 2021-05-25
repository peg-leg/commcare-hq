from django.utils.translation import ugettext_lazy as _


FHIR_VERSION_4_0_1 = '4.0.1'
FHIR_VERSIONS = [
    (FHIR_VERSION_4_0_1, 'R4'),
]

# See https://www.hl7.org/fhir/valueset-bundle-type.html
FHIR_BUNDLE_TYPES = {
    'document',
    'message',
    'transaction',
    'transaction-response',
    'batch',
    'batch-response',
    'history',
    'searchset',
    'collection',
}

# The XMLNS to use for forms for data imported from a remote FHIR
# service. e.g. Setting `external_id` on a case to its FHIR ID, or cases
# created or updated in CommCare via our FHIR API. This is to prevent
# FHIRRepeater from sending data from a FHIR service back again.
XMLNS_FHIR = 'http://commcarehq.org/x/fhir/engine-read'

# The URI to identify CommCare as the system responsible for allocating
# case IDs. See https://www.hl7.org/fhir/datatypes.html#Identifier
SYSTEM_URI_CASE_ID = 'http://commcarehq.org/x/fhir/case-id'

IMPORT_FREQUENCY_DAILY = 'daily'
IMPORT_FREQUENCY_CHOICES = (
    (IMPORT_FREQUENCY_DAILY, _('Daily')),
)

FHIR_DATA_TYPE_LIST_OF_STRING = 'fhir_list_of_string'
FHIR_DATA_TYPES = (
    FHIR_DATA_TYPE_LIST_OF_STRING,
)

SUPPORTED_FHIR_RESOURCE_TYPES = [
    'Patient',
    'DiagnosticReport',
    'Observation'
]
