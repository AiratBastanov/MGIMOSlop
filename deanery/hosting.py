"""Exact HTTPS origins assigned by the supported container hosts."""
import os
import re


def assigned_hostname(provider):
    domains = {
        'render': ('RENDER_EXTERNAL_HOSTNAME', 'onrender.com'),
        'host67': ('HOST67_EXTERNAL_HOSTNAME', 'host67.com'),
    }
    if provider not in domains:
        raise ValueError('Неизвестная площадка размещения.')
    variable, domain = domains[provider]
    hostname = os.environ.get(variable, '')
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if not re.fullmatch(label + r'\.' + re.escape(domain), hostname):
        raise ValueError(f'Укажите выданный площадкой домен в {variable}.')
    return hostname
