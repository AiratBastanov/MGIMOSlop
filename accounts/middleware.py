from django.contrib.sessions.middleware import SessionMiddleware
from django.db import DatabaseError, transaction
from django.shortcuts import render
from .access import AccessDenied, current_actor, lock_guard
from django.conf import settings


class GuardedSessionMiddleware(SessionMiddleware):
    def process_response(self, request, response):
        try:
            session = getattr(request,'session',None)
            if session is not None and session.modified and session.get('context_id'):
                with transaction.atomic():
                    guard = lock_guard()
                    current_actor(request,guard=guard,lock=True)
                    return super().process_response(request,response)
            return super().process_response(request,response)
        except AccessDenied:
            # Не сохранять старую session и не отправлять Set-Cookie запоздалому ответу.
            request.session.modified = False
            return render(request,'error.html',{'message':'Сеанс изменился. Откройте страницу заново.'},status=409)
        except DatabaseError:
            return render(request,'error.html',{'message':'Кабинет временно недоступен. Попробуйте ещё раз.'},status=503)


class ResponsePolicyMiddleware:
    def __init__(self,get_response): self.get_response = get_response
    def __call__(self,request):
        response = self.get_response(request)
        if getattr(settings, 'DEVELOPMENT_BUILD', ''):
            response['X-Deanery-Build'] = settings.DEVELOPMENT_BUILD
        if not request.path.startswith('/static/'):
            response['Cache-Control'] = 'private, no-store, max-age=0'
            response['Pragma'] = 'no-cache'
            response['Referrer-Policy'] = 'same-origin'
            response['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response
