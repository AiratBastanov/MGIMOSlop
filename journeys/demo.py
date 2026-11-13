"""Explicit synthetic journey preparation through the same services as the UI."""
import json
import uuid
from pathlib import Path
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.test import RequestFactory
from accounts.access import authenticate_session,current_actor,lock_guard,logout_session,identity_for
from accounts.models import Identity
from .models import JourneyOperation,Journey
from .services import perform

NAMESPACE=uuid.UUID('a31522a8-ab8f-4f1a-8dc4-67d62cb38542')
EDITION='connected-preparation-demo-v1'
def key(name):return str(uuid.uuid5(NAMESPACE,name))

def provision(credentials_path,*,operator):
    if not operator or len(operator)>100: raise ValueError('Укажите оператора.')
    with transaction.atomic():
        lock_guard()
        identity=Identity.objects.get(user__username='student_a')
        _,identity=identity_for(identity.user_id)
        old=JourneyOperation.objects.filter(user=identity.user,key=key('complete')).first()
        if old:
            if old.fingerprint!=EDITION or not Journey.objects.filter(pk=old.journey_id,owner=identity.profile,template_key='exchange').exists():
                raise ValueError('Конфликт сохранённой подготовки демонстрации.')
            return {'state':'noop',**old.outcome}
        if Journey.objects.filter(owner=identity.profile,template_key='exchange').exists():
            raise ValueError('У студента уже есть собственная подготовка; синтетическая версия её не заменяет.')
        credentials=json.loads(Path(credentials_path).read_text(encoding='utf-8'))
        request=RequestFactory().post('/explicit-preparation-demo/');request.session=SessionStore()
        if authenticate_session(request,'student_a',credentials['student_a'])!='student': raise ValueError('Не совпала роль студента.')
        actor=current_actor(request)
        result=perform(request,actor.marker,{'action':'start','template':'exchange','operation':key('start')})
        journey=Journey.objects.get(pk=result['id']);state=journey.state
        state['checks']['requirements']=True
        state['documents']['cv_motivation']={'need':'clarify','source':'Демо-условия «Горизонта» не задают обязательный состав: уточнить у программы.','prepared':False}
        state['documents']['learning_agreement']={'need':'required','source':'Вымышленное условие примера: подготовить проект учебного плана для обсуждения.','prepared':False}
        state['questions']='Какие дисциплины стоит обсудить и где уточнить требования к выписке об успеваемости?'
        state['private_note']='Моя синтетическая личная заметка: сначала сформулировать учебные вопросы.'
        result=perform(request,actor.marker,{'action':'save','version':journey.version,'state':state,'operation':key('save')},journey.pk)
        from consultations.models import Booking
        booking=Booking.objects.filter(student=identity.profile,state='booked').order_by('-pk').first()
        if booking:perform(request,actor.marker,{'action':'link_booking','version':result['version'],'booking':booking.pk,'operation':key('link')},journey.pk)
        outcome={'journey':journey.pk,'edition':EDITION,'operator':operator}
        JourneyOperation.objects.create(user=identity.user,key=key('complete'),fingerprint=EDITION,journey=journey,outcome=outcome)
        logout_session(request,current_actor(request).marker)
        return {'state':'created',**outcome}
