"""Единственное рабочее определение переходов и доступных действий S5."""
from dataclasses import dataclass


REQUEST_TYPE = 'study_certificate'
TYPE_LABEL = 'Справка об обучении'
REQUEST_TYPES = {
    'study_certificate': 'Справка об обучении',
    'academic_correction': 'Исправление учебных данных',
}
CERTIFICATE_PURPOSES = {
    'scholarship': 'Для стипендии или льготы',
    'employment': 'Для работодателя',
    'other': 'По месту требования',
}
CORRECTION_AREAS = {
    'result': 'Результат или учебное требование',
    'schedule': 'Расписание',
    'profile': 'Учебные сведения профиля',
}
PAYLOAD_FIELDS = {
    'study_certificate': ('purpose', 'recipient', 'copies'),
    'academic_correction': ('correction_area', 'record_label', 'requested_change'),
}
FIELD_LABELS = {
    'purpose': 'Назначение', 'recipient': 'Куда требуется справка', 'copies': 'Количество экземпляров',
    'correction_area': 'Какие сведения', 'record_label': 'Дисциплина или поле',
    'requested_change': 'Что нужно проверить и исправить',
}
TEXT_LIMIT = 500
READY_NOTICE = 'Обработка в демонстрации завершена; действительная справка не выпускается'
READY_NOTICES = {
    'study_certificate': READY_NOTICE,
    'academic_correction': 'Обращение рассмотрено в демонстрации; учебные сведения меняются только проверенным импортом',
}
STATUS_LABELS = {
    'submitted': 'Принята',
    'in_progress': 'В работе',
    'rejected': 'Отклонена',
    'ready': 'Готова',
}
TERMINAL = frozenset({'rejected', 'ready'})


@dataclass(frozen=True)
class Transition:
    action: str
    from_states: tuple[str, ...]
    to_status: str
    label: str
    explanation_label: str = ''

    def values(self):
        return {
            'action': self.action, 'label': self.label,
            'to_status': self.to_status,
            'explanation_label': self.explanation_label,
            'requires_explanation': bool(self.explanation_label),
        }


TRANSITIONS = {
    'take': Transition('take', ('submitted',), 'in_progress', 'Взять в работу'),
    'complete': Transition('complete', ('in_progress',), 'ready', 'Завершить обработку',
                           'Пояснение демонстрационного результата'),
    'reject': Transition('reject', ('submitted', 'in_progress'), 'rejected', 'Отклонить заявку',
                         'Причина отклонения'),
}


def available_actions(status):
    return [transition.values() for transition in TRANSITIONS.values()
            if status in transition.from_states]
