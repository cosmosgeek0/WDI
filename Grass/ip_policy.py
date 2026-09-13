"""Check this container's public IP and request its own reallocation on datacenter evidence."""
import ipaddress
import json
import time
import urllib.error
import urllib.request

from session import IP_POLICY, locked, public_ip, read, runtime, save


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def check():
    scope = runtime()
    now = time.clock_gettime(time.CLOCK_BOOTTIME)
    try:
        old = read(IP_POLICY)
    except (OSError, ValueError):
        old = {}
    if old.get('scope') != scope:
        old = {}
    if old.get('reallocation_requested'):
        return old
    result = dict(scope=scope, ip=None, source='IPQuery risk.is_datacenter',
                  is_datacenter=None, allowed=False, valid_until=now+75,
                  ip_checked_ms=int(time.time()*1000))
    try:
        address = public_ip()['public_ip']
        result['ip'] = address
        if old.get('ip')==address:
            result.update({k:old[k] for k in ('reallocation_retry_at','reallocation_rejected') if k in old})
        if old.get('ip') == address and now < old.get('classification_until', 0):
            result.update({k:old[k] for k in ('is_datacenter', 'allowed', 'observed_ms', 'country',
                                              'classification_until', 'error') if k in old})
        else:
            query = urllib.request.Request('https://api.ipquery.io/'+address+'?format=json',
                                           headers={'User-Agent':'WDI/1.0','Accept':'application/json'})
            with urllib.request.build_opener(NoRedirect()).open(query, timeout=8) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('ip_classification_response_too_large')
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get('ip'), str):
                raise ValueError('ip_classification_unknown')
            if ipaddress.ip_address(data['ip']) != ipaddress.ip_address(address):
                raise ValueError('ip_classification_identity_mismatch')
            risk = data.get('risk')
            flag = risk.get('is_datacenter') if isinstance(risk, dict) else None
            if type(flag) is not bool:
                raise ValueError('ip_classification_unknown')
            location = data.get('location')
            country = location.get('country_code') if isinstance(location, dict) else None
            country = country if (isinstance(country, str) and len(country) == 2
                                  and country.isascii() and country.isalpha() and country.isupper()) else None
            result.update(is_datacenter=flag, allowed=not flag, observed_ms=int(time.time()*1000),
                          classification_until=now+3600, country=country)
    except (OSError, ValueError, KeyError, TypeError) as error:
        retry = 60
        if isinstance(error, urllib.error.HTTPError):
            after = error.headers.get('Retry-After', '')
            if after.isdigit():
                retry = max(retry, min(3600, int(after)))
        result.update(error=str(error) if type(error) is ValueError else type(error).__name__,
                      observed_ms=int(time.time()*1000), classification_until=now+retry)
    with locked():
        # An uncertain reallocation request keeps this runtime held permanently.
        current = read(IP_POLICY) if IP_POLICY.exists() else {}
        if current.get('scope') == scope and current.get('reallocation_requested'):
            return current
        save(IP_POLICY, result)
    return result


def reallocate(policy):
    if public_ip()['public_ip'] != policy.get('ip'):
        raise ValueError('public_ip_changed_before_reallocation')
    with locked():
        current = read(IP_POLICY)
        if current.get('scope') != runtime() or current.get('is_datacenter') is not True:
            raise ValueError('datacenter_evidence_required')
        if current.get('ip') != policy.get('ip'):
            raise ValueError('ip_classification_changed')
        if current.get('reallocation_requested'):
            return current
        reason = 'WDI confirmed datacenter IP '+current['ip']+' (IPQuery risk.is_datacenter=true)'
        current.update(reallocation_requested=True, reallocation_requested_ms=int(time.time()*1000),
                       allowed=False, reason=reason)
        save(IP_POLICY, current)
    # No Salad account key is required. A lost response is not retried blindly.
    query = urllib.request.Request('http://169.254.169.254/v1/reallocate',
        data=json.dumps({'reason':reason}).encode(), method='POST',
        headers={'Metadata':'true','Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(query, timeout=8) as response:
            if response.status != 204:
                raise ValueError('unexpected_reallocation_response')
        current['reallocation_accepted'] = True
    except urllib.error.HTTPError as error:
        # A definitive rejection did not schedule a move. Recheck the IP and
        # retry only after the server's backoff; retain holds on lost replies.
        after = error.headers.get('Retry-After','')
        delay = max(30,min(3600,int(after))) if after.isdigit() else 60
        current['error']='reallocation_http_'+str(error.code)
        if error.code in (400,403,404,429):
            current.update(reallocation_requested=False,reallocation_accepted=False,
                           reallocation_rejected=error.code!=429,
                           reallocation_retry_at=time.clock_gettime(time.CLOCK_BOOTTIME)+delay)
        else:
            current['reallocation_accepted']=None
    except (OSError, ValueError) as error:
        current.update(reallocation_accepted=None,
                       error=str(error) if type(error) is ValueError else type(error).__name__)
    with locked():
        save(IP_POLICY, current)
    return current
