"""Личная связь; вызывается внутри транзакции владельца операции."""
from django.db import connection
from accounts.access import AccessDenied
from .models import Journey, JourneyBooking
from .templates import TEMPLATES


def owned_journey(actor, value):
    if actor.role != 'student' or type(value) not in (str, int):
        raise AccessDenied('not_found', 404)
    raw = str(value)
    if not 1 <= len(raw) <= 10 or not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 2147483647:
        raise AccessDenied('not_found', 404)
    row = Journey.objects.filter(pk=int(raw), owner_id=actor.profile_id).first()
    if not row:
        raise AccessDenied('not_found', 404)
    return row


def context(row):
    return {'id': row.pk, 'title': TEMPLATES[(row.template_key, row.template_revision)]['title'],
            'url': f'/journeys/{row.pk}/'}


def associate(actor, journey, booking):
    # Общий guard уже удерживается сервисом; добавление не меняет план/ревизию.
    assert connection.in_atomic_block
    if actor.role != 'student' or journey.owner_id != actor.profile_id or booking.student_id != actor.profile_id:
        raise AccessDenied('not_found', 404)
    return JourneyBooking.objects.get_or_create(journey=journey, booking=booking)[0]
