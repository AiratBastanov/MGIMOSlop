def hosted_https(get_response):
    # The public edge must redirect HTTP before requests reach this container.
    # Ignore forwarded scheme and client address headers from visitors.
    def middleware(request):
        request.META['wsgi.url_scheme'] = 'https'
        request.get_host()  # Enforce the assigned hostname even for WhiteNoise.
        return get_response(request)
    return middleware
