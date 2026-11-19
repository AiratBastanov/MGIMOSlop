def render_https(get_response):
    # Render Free redirects public HTTP at its edge and accepts no private ingress.
    # Set the public scheme from that deployment contract, before SecurityMiddleware.
    def middleware(request):
        request.META['wsgi.url_scheme'] = 'https'
        request.get_host()  # Enforce the assigned hostname even for WhiteNoise.
        return get_response(request)
    return middleware
