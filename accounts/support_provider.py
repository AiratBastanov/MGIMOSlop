"""Bounded local provider access. Availability is separate from answer provenance."""
import http.client
import ipaddress
import json
import logging
import socket
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings

STATUS_TIMEOUT = 2.0
INFERENCE_TIMEOUT = 10.0
CACHE_SECONDS = 30.0
RECHECK_COOLDOWN = 2.0
FAILURE_COOLDOWN = 5.0
MAX_BYTES = 32768
_lock = threading.Lock()
_key = None
_cached = None
_checked = 0.0
_checking = False
logger = logging.getLogger(__name__)


class ProviderFailure(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def configured():
    return bool(settings.SUPPORT_MODEL and settings.SUPPORT_PROVIDER in ('ollama', 'openai_local')
                and settings.SUPPORT_LOCAL_ONLY_CONFIRMED)


def configuration():
    if not configured():
        return None
    try:
        endpoint = urlsplit(settings.SUPPORT_ENDPOINT)
        host = endpoint.hostname or ''
        local = ipaddress.ip_address(host).is_loopback
        port = endpoint.port or 80
    except ValueError as exc:
        raise ProviderFailure('invalid_configuration') from exc
    if (endpoint.scheme != 'http' or not local or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path not in ('', '/')
            or any(tag in settings.SUPPORT_MODEL.lower() for tag in ('cloud', '/', 'http'))):
        raise ProviderFailure('invalid_configuration')
    return settings.SUPPORT_PROVIDER, host, port, settings.SUPPORT_MODEL


def status_value(state, reason=''):
    return {'state': state, 'reason': reason}


def request_json(config, method, path, payload=None, *, timeout):
    """No DNS, proxy, redirects or raw provider diagnostics. Hard wall-clock bound."""
    _, host, port, _ = config
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    expired = threading.Event()
    transport = [None]

    def expire():
        expired.set()
        sock = transport[0]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        transport[0] = conn.sock
        if expired.is_set():
            raise ProviderFailure('timeout')
        conn.request(method, path, body=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
                     headers={'Content-Type': 'application/json', 'Connection': 'close'})
        response = conn.getresponse()
        if response.status != 200:
            reason = 'model_missing' if response.status == 404 and method == 'POST' else 'provider_error'
            raise ProviderFailure(reason)
        raw = response.read(MAX_BYTES + 1)
        if expired.is_set():
            raise ProviderFailure('timeout')
        if len(raw) > MAX_BYTES:
            raise ProviderFailure('invalid_response')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ProviderFailure('invalid_response') from exc
        if not isinstance(data, dict):
            raise ProviderFailure('invalid_response')
        return data
    except (OSError, http.client.HTTPException) as exc:
        raise ProviderFailure('timeout' if expired.is_set() or isinstance(exc, TimeoutError) else 'service_unavailable') from exc
    finally:
        timer.cancel()
        conn.close()


def availability(*, probe=False, refresh=False):
    global _key, _cached, _checked, _checking
    try:
        config = configuration()
    except ProviderFailure as exc:
        return status_value('disabled', exc.reason)
    if config is None:
        return status_value('disabled', 'unconfigured')
    now = time.monotonic()
    with _lock:
        if _key != config:
            _key, _cached, _checked, _checking = config, None, 0.0, False
        ttl = FAILURE_COOLDOWN if _cached and _cached['state'] == 'inference_failed' else CACHE_SECONDS
        if _cached and now - _checked < (RECHECK_COOLDOWN if refresh else ttl):
            return dict(_cached)
        if not probe or _checking:
            return status_value('checking')
        _checking = True
    try:
        data = request_json(config, 'GET', '/api/tags' if config[0] == 'ollama' else '/v1/models', timeout=STATUS_TIMEOUT)
        models = data.get('models' if config[0] == 'ollama' else 'data')
        if not isinstance(models, list) or any(not isinstance(row, dict) for row in models):
            raise ProviderFailure('invalid_response')
        names = [row.get('name', row.get('model')) if config[0] == 'ollama' else row.get('id') for row in models]
        if any(not isinstance(name, str) or not name for name in names):
            raise ProviderFailure('invalid_response')
        result = status_value('available') if config[3] in names else status_value('model_missing', 'model_missing')
    except ProviderFailure as exc:
        result = status_value('unavailable', exc.reason)
        logger.info('support_status:%s', exc.reason)
    finally:
        with _lock:
            if _key == config:
                _checking = False
    with _lock:
        if _key == config:
            _cached, _checked = result, time.monotonic()
    return dict(result)


def inference_failed(reason):
    global _cached, _checked
    result = status_value('model_missing' if reason == 'model_missing' else 'inference_failed', reason)
    with _lock:
        _cached, _checked = result, time.monotonic()
    logger.info('support_inference:%s', reason)
    return dict(result)


def clear_status_cache():
    """Isolate controlled provider tests; production uses expiry/manual recheck."""
    global _key, _cached, _checked, _checking
    with _lock:
        _key, _cached, _checked, _checking = None, None, 0.0, False
