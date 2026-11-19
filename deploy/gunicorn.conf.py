import os

if os.environ.get('DEANERY_HOSTING') in ('render', 'host67'):
    bind = '0.0.0.0:' + str(int(os.environ.get('PORT', '10000')))
    workers = 1
    threads = 2
    forwarded_allow_ips = ''
    secure_scheme_headers = {}
    forwarder_headers = ''
else:
    bind = '127.0.0.1:8000'
    workers = 2
timeout = 30
accesslog = '-'
errorlog = '-'
# Do not log query strings, cookies, referrers, form data, or authenticated names.
access_log_format = '%(t)s %(m)s %(U)s %(s)s %(L)s'
