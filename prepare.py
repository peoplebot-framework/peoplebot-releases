#!/usr/bin/env python3
"""Validate a private enrollment request offline; never provision or transmit it.

SPDX-License-Identifier: GPL-3.0-only
PeopleBot was created by Guthrie E Services, LLC.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

FORMAT = 'peoplebot.syslog.enrollment.v0'
FIELDS = {'format', 'project', 'environment', 'repositories', 'events', 'destination', 'gateway_url'}
IDENTIFIER = r'[a-z][a-z0-9-]{0,62}'

def require(condition, message):
    if not condition:
        raise ValueError(message)

def validate(request):
    require(isinstance(request, dict) and set(request) == FIELDS, 'Unexpected or missing fields; secrets and free-form instructions are not permitted')
    require(request['format'] == FORMAT, 'Unsupported request format')
    for key in ('project', 'environment'):
        require(isinstance(request[key], str) and re.fullmatch(IDENTIFIER, request[key]), 'Invalid project/environment identifier')
    repos = request['repositories']
    require(isinstance(repos, list) and 0 <= len(repos) <= 16, 'At most sixteen repositories supported')
    ids, names = set(), set()
    for repo in repos:
        require(isinstance(repo, dict) and set(repo) == {'id', 'full_name'}, 'Repository needs numeric ID and full name only')
        require(type(repo['id']) is int and repo['id'] > 0, 'Invalid repository ID')
        name = repo['full_name']
        require(isinstance(name, str) and len(name) <= 200 and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name), 'Invalid repository full name')
        require(repo['id'] not in ids and name.lower() not in names, 'Duplicate repository binding')
        ids.add(repo['id']); names.add(name.lower())
    events = request['events']
    require(isinstance(events, list) and 1 <= len(events) <= 64 and all(isinstance(e, str) for e in events), 'Invalid event selection')
    require(len(set(events)) == len(events), 'Duplicate event selection')
    require(events == ['*'] or all(re.fullmatch(r'[a-z_]{1,64}', e) for e in events), 'Use either all events or explicit event names')
    destination = request['destination']
    if destination is not None:
        require(isinstance(destination, dict) and set(destination) == {'kind', 'host', 'port'}, 'Invalid destination schema')
        require(destination['kind'] == 'papertrail_tls', 'Only verified TLS syslog is supported')
        require(isinstance(destination['host'], str) and re.fullmatch(r'logs\d*\.papertrailapp\.com', destination['host']), 'Use assigned Papertrail hostname')
        require(type(destination['port']) is int and 1 <= destination['port'] <= 65535, 'Invalid destination port')
    url = request['gateway_url']
    if url is not None:
        require(isinstance(url, str) and len(url) <= 512 and url.isascii() and not any(c.isspace() for c in url), 'Invalid gateway URL')
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.username is None and parsed.password is None and parsed.port in (None,443), 'Gateway requires HTTPS without credentials')
        host = parsed.hostname or ''
        require('.' in host and re.fullmatch(r'[a-z0-9.-]+', host) and not host.endswith('.'), 'Gateway requires a DNS hostname')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('Gateway must use its verified DNS name, not an IP')
        require(not parsed.query and not parsed.fragment and parsed.path == '/webhooks/github/' + request['project'], 'Gateway route must match project and contain no query/fragment')
    return request

def plan(request):
    validate(request)
    missing = []
    if not request['repositories']: missing.append('approved_repository_bindings')
    if request['destination'] is None: missing.append('papertrail_tls_destination')
    if request['gateway_url'] is None: missing.append('operator_assigned_gateway_url')
    canonical = json.dumps(request, sort_keys=True, separators=(',', ':')).encode()
    return {'format':'peoplebot.syslog.preparation.v0',
            'request_sha256':hashlib.sha256(canonical).hexdigest(),
            'status':'prepared_waiting_inputs' if missing else 'prepared_for_operator_review',
            'missing':missing, 'activation_authorized':False,
            'next_step':'Complete missing bindings when available; then use the existing authorized private operator channel. No automatic activation.'}

def load(path):
    with Path(path).open('rb') as stream:
        data = stream.read(65537)
    require(len(data) <= 65536, 'Request exceeds 64 KiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    return validate(json.loads(data, object_pairs_hook=unique))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request', required=True, help='Private JSON enrollment request')
    p.add_argument('--output', help='New private preparation report; refuses to overwrite')
    args = p.parse_args()
    try:
        result = plan(load(args.request))
        encoded = json.dumps(result, indent=2) + '\n'
        if args.output:
            fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd,'w') as f:
                f.write(encoded); f.flush(); os.fsync(f.fileno())
        print(encoded, end='')
    except (OSError, ValueError, TypeError, RecursionError):
        # Don't echo untrusted input values, paths or exception contents.
        p.exit(2, 'Preparation failed: invalid request or unavailable output. No activation performed.\n')

if __name__ == '__main__':
    main()
