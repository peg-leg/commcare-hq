from functools import wraps

from django.http import HttpResponseBadRequest, HttpResponseForbidden, Http404

from corehq.apps.linked_domain.dbaccessors import get_upstream_domain_link
from corehq.apps.linked_domain.util import can_user_access_linked_domains

REMOTE_REQUESTER_HEADER = 'HTTP_HQ_REMOTE_REQUESTER'


def require_access_to_linked_domains(view_func):
    @wraps(view_func)
    def _inner(request, domain, *args, **kwargs):
        if not hasattr(request, 'couch_user'):
            raise Http404()

        couch_user = request.couch_user

        def call_view():
            return view_func(request, domain, *args, **kwargs)
        if can_user_access_linked_domains(couch_user, domain):
            return call_view()
        else:
            return HttpResponseForbidden()

    return _inner


def require_linked_domain(fn):
    @wraps(fn)
    def _inner(request, domain, *args, **kwargs):
        requester = request.META.get(REMOTE_REQUESTER_HEADER, None)
        if not requester:
            return HttpResponseBadRequest()

        link = get_upstream_domain_link(requester)
        if not link or link.master_domain != domain:
            return HttpResponseForbidden()

        return fn(request, domain, *args, **kwargs)

    return _inner
