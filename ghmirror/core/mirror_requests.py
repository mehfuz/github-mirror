# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright: Red Hat Inc. 2020
# Author: Amador Pahim <apahim@redhat.com>

"""
Implements conditional requests
"""

import hashlib
import logging

import requests

from ghmirror.core.constants import REQUESTS_TIMEOUT, PER_PAGE_ELEMENTS
from ghmirror.data_structures.monostate import GithubStatus
from ghmirror.data_structures.requests_cache import RequestsCache
from ghmirror.decorators.metrics import requests_metrics


logging.basicConfig(level=logging.INFO,
                    format='%(asctime)-15s %(message)s')
LOG = logging.getLogger(__name__)


def _get_elements_per_page(url_params):
    """
    Get 'per_page' parameter if present in URL or
    return None if not present
    """
    if url_params is not None:
        per_page = url_params.get('per_page')
        if per_page is not None:
            return int(per_page)

    return None


def _cache_response(resp, cache, cache_key):
    """
    Implements the logic to decide whether or not
    whe should cache a request acording to the headers
    and content
    """
    # Caching only makes sense when at least one
    # of those headers is present
    if resp.status_code == 200 and \
       any(['ETag' in resp.headers, 'Last-Modified' in resp.headers]):
        cache[cache_key] = resp


@requests_metrics
def conditional_request(method, url, auth, data=None, url_params=None):
    """
    Implements conditional requests, checking first whether
    the upstream API is online of offline to decide which
    request routine to call.
    """
    if GithubStatus().online:
        return online_request(method, url, auth, data, url_params)
    return offline_request(method, url, auth)


def online_request(method, url, auth, data=None, url_params=None):
    """
    Implements conditional requests.
    """
    cache = RequestsCache()
    headers = {}
    parameters = url_params.to_dict() if url_params is not None else {}

    per_page_elements = _get_elements_per_page(url_params)

    if per_page_elements is None:
        per_page_elements = PER_PAGE_ELEMENTS
        parameters['per_page'] = PER_PAGE_ELEMENTS

    if auth is None:
        auth_sha = None
    else:
        auth_sha = hashlib.sha1(auth.encode()).hexdigest()
        headers['Authorization'] = auth

    # Special case for non-GET requests
    if method != 'GET':
        # Just forward the request with the auth header
        resp = requests.request(method=method,
                                url=url,
                                headers=headers,
                                data=data,
                                timeout=REQUESTS_TIMEOUT,
                                params=parameters)

        LOG.info('ONLINE %s CACHE_MISS %s', method, url)
        # And just forward the response (with the
        # cache-miss header, for metrics)
        resp.headers['X-Cache'] = 'ONLINE_MISS'
        return resp

    cache_key = (url, auth_sha)

    cached_response = None
    if cache_key in cache:
        cached_response = cache[cache_key]
        etag = cached_response.headers.get('ETag')
        if etag is not None:
            headers['If-None-Match'] = etag
        last_mod = cached_response.headers.get('Last-Modified')
        if last_mod is not None:
            headers['If-Modified-Since'] = last_mod

    resp = requests.request(method=method,
                            url=url,
                            headers=headers,
                            timeout=REQUESTS_TIMEOUT,
                            params=parameters)

    if resp.status_code == 304:
        if len(cached_response.json()) == per_page_elements and \
           not cached_response.links:

            headers.pop('If-None-Match')
            resp = requests.request(method=method,
                                    url=url,
                                    headers=headers,
                                    timeout=REQUESTS_TIMEOUT,
                                    params=parameters)

            LOG.info('ONLINE GET CACHE_MISS %s', url)
            resp.headers['X-Cache'] = 'ONLINE_MISS'
            _cache_response(resp, cache, cache_key)
            return resp

        LOG.info('ONLINE GET CACHE_HIT %s', url)
        cached_response.headers['X-Cache'] = 'ONLINE_HIT'
        return cached_response

    # When we hit the API limit, let's try to serve from cache
    if _should_serve_from_cache(resp):
        if cached_response is None:
            LOG.info('RATE_LIMITED GET CACHE_MISS %s', url)
            resp.headers['X-Cache'] = 'RATE_LIMITED_MISS'
            return resp
        LOG.info('RATE_LIMITED GET CACHE_HIT %s', url)
        cached_response.headers['X-Cache'] = 'RATE_LIMITED_HIT'
        return cached_response

    LOG.info('ONLINE GET CACHE_MISS %s', url)
    resp.headers['X-Cache'] = 'ONLINE_MISS'
    _cache_response(resp, cache, cache_key)
    return resp


def _should_serve_from_cache(response):
    """Try to serve response from the cache when we hit API limit

    Args:
        response (Response): requests module response
    """
    rate_limit_messages = {
        'API rate limit exceeded',
        'abuse detection mechanism'
    }
    return response.status_code == 403 and \
        any(m in response.text for m in rate_limit_messages)


def offline_request(method, url, auth, error_code=504,
                    error_message=b'{"message": "gateway timeout"}\n'):
    """
    Implements offline requests (serves content from cache, when possible).
    """
    headers = {}
    if auth is None:
        auth_sha = None
    else:
        auth_sha = hashlib.sha1(auth.encode()).hexdigest()
        headers['Authorization'] = auth

    # Special case for non-GET requests
    if method != 'GET':
        LOG.info('OFFLINE %s CACHE_MISS %s', method, url)
        # Not much to do here. We just build up a response
        # with a reasonable status code so users know that our
        # upstream is offline
        response = requests.models.Response()
        response.status_code = error_code
        response.headers['X-Cache'] = 'OFFLINE_MISS'
        # pylint: disable=protected-access
        response._content = error_message
        return response

    cache = RequestsCache()
    cache_key = (url, auth_sha)
    if cache_key in cache:
        LOG.info('OFFLINE GET CACHE_HIT %s', url)
        # This is the best case: upstream is offline
        # but we have the resource in cache for a given
        # user. We then serve from cache.
        cached_response = cache[cache_key]
        cached_response.headers['X-Cache'] = 'OFFLINE_HIT'
        return cached_response

    LOG.info('OFFLINE GET CACHE_MISS %s', url)
    # GETs without cached content will receive an error
    # code so they know our upstream is offline.
    response = requests.models.Response()
    response.status_code = error_code
    response.headers['X-Cache'] = 'OFFLINE_MISS'
    # pylint: disable=protected-access
    response._content = error_message
    return response
