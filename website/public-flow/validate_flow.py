"""Strict public boundary: fixed keys, strings, numeric leaves and arithmetic."""
import math
import re

MINUTE = 60000
MAX_BYTES = 256 * 1024


def num(v): return type(v) in (int, float) and math.isfinite(v) and 0 <= v < 2**53

def integer(v): return type(v) is int and 0 <= v < 2**53

def stamp(v): return integer(v) and v > 0

def exact(v, keys): return type(v) is dict and set(v) == set(keys.split())

def near(a, b): return num(a) and num(b) and math.isclose(a, b, abs_tol=2e-10, rel_tol=1e-10)

def maybe(v, check): return v is None or check(v)


def history_periods(points, end):
    for hours in (1, 6, 12, 24):
        chosen = points[-hours * 60:]
        known = [p for p in chosen if p[1] is not None]
        yield known, dict(hours=hours, start_ms=chosen[0][0], end_ms=end,
                          observed_minutes=len(known), missing_minutes=len(chosen)-len(known),
                          partial_minutes=sum(p[3] for p in known), estimated_minutes=sum(p[4] for p in known))


def validate(v, now):
    if not exact(v, 'schema_version generated_ms revision scope source recorded_traffic ips traffic_24h periods'): return False
    if type(v['schema_version']) is not int or v['schema_version'] != 1 or v['scope'] != 'retained_network_recordings': return False
    if not stamp(v['generated_ms']) or v['generated_ms'] > now: return False
    if type(v['revision']) is not str or not re.fullmatch('[0-9a-f]{64}', v['revision']): return False
    generated = v['generated_ms']
    date = lambda x: stamp(x) and x <= generated
    s, r, c, chart = v['source'], v['recorded_traffic'], v['ips'], v['traffic_24h']
    if not exact(s, 'observed_ms valid_until_ms status') or s['status'] not in ('current','stale','unavailable'): return False
    if not maybe(s['observed_ms'], date) or not maybe(s['valid_until_ms'], stamp): return False
    if s['observed_ms'] is None:
        if s['valid_until_ms'] is not None or s['status'] != 'unavailable': return False
    elif s['valid_until_ms'] != s['observed_ms'] + 90000: return False
    if s['status'] == 'current' and (s['observed_ms'] is None or s['valid_until_ms'] < generated): return False
    if not exact(r, 'gb start_ms end_ms coverage status') or r['coverage'] != 'partial_recordings' or r['status'] not in ('recorded','partial','unavailable'): return False
    if not maybe(r['gb'],num) or not maybe(r['start_ms'],date) or not maybe(r['end_ms'],date): return False
    if r['gb'] is None:
        if r['status'] != 'unavailable' or r['start_ms'] is not None or r['end_ms'] is not None: return False
    elif r['start_ms'] is None or r['end_ms'] is None or r['start_ms'] > r['end_ms'] or r['status'] == 'unavailable': return False
    if not exact(c, 'all_recorded active known_active active_without_ip observed_ms valid_until_ms status'): return False
    if c['status'] not in ('current','partial','stale','unavailable'): return False
    if not all(maybe(c[k],integer) for k in ('all_recorded','active','known_active','active_without_ip')): return False
    if not maybe(c['observed_ms'],date) or not maybe(c['valid_until_ms'],stamp): return False
    if c['status'] in ('current','partial'):
        if not all(integer(c[k]) for k in ('known_active','active_without_ip')) or not date(c['observed_ms']) or not stamp(c['valid_until_ms']): return False
        if not generated <= c['valid_until_ms'] <= c['observed_ms']+90000 or s['status'] != 'current': return False
        if c['status']=='current' and (c['active'] != c['known_active'] or c['active_without_ip'] != 0): return False
        if c['status']=='partial' and (c['active'] is not None or c['active_without_ip'] < 1): return False
    elif any(c[k] is not None for k in ('active','known_active','active_without_ip')): return False
    if not exact(chart, 'start_ms end_ms bucket_ms points') or chart['bucket_ms'] != MINUTE: return False
    end = generated//MINUTE*MINUTE + MINUTE
    if chart['end_ms'] != end or chart['start_ms'] != end-1440*MINUTE: return False
    points = chart['points']
    if type(points) is not list or len(points) != 1440: return False
    for i, p in enumerate(points):
        if type(p) is not list or len(p) != 5 or p[0] != chart['start_ms']+i*MINUTE: return False
        if not maybe(p[1],num) or not integer(p[2]) or type(p[3]) is not bool or type(p[4]) is not bool: return False
        if p[1] is None and (p[2] != 0 or p[3] is not True or p[4] is not False): return False
    if points[-1][3] is not True: return False
    if type(v['periods']) is not list or len(v['periods']) != 4: return False
    for period, (known, counts) in zip(v['periods'], history_periods(points, end)):
        if not exact(period,'hours start_ms end_ms gb distinct_ips observed_minutes missing_minutes partial_minutes estimated_minutes'): return False
        if not all(integer(period[k]) and period[k] == expected for k, expected in counts.items()): return False
        if not known:
            if period['gb'] is not None or period['distinct_ips'] is not None: return False
        elif not near(period['gb'],sum(p[1] for p in known)) or not integer(period['distinct_ips']) or not max(p[2] for p in known) <= period['distinct_ips'] <= sum(p[2] for p in known): return False
    return True
