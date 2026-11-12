bind = '127.0.0.1:8000'
workers = 2
timeout = 30
accesslog = '-'
errorlog = '-'
# Do not log query strings, cookies, referrers, form data, or authenticated names.
access_log_format = '%(t)s %(m)s %(U)s %(s)s %(L)s'
