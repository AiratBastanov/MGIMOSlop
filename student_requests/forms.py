"""Представление ввода; нормализация и предметная проверка находятся в services."""
from django import forms
from .workflow import CERTIFICATE_PURPOSES, CORRECTION_AREAS, REQUEST_TYPE


class RequestForm(forms.Form):
    comment = forms.CharField(label='Комментарий (необязательно)', required=False, strip=False,
        widget=forms.Textarea(attrs={'rows': 2, 'aria-describedby': 'comment-help', 'data-sensitive-draft': ''}))

    def __init__(self, *args, request_type=REQUEST_TYPE, **kwargs):
        super().__init__(*args, **kwargs)
        attrs = {'data-sensitive-draft': ''}
        if request_type == 'study_certificate':
            fields = {
                'purpose': forms.ChoiceField(label='Назначение', choices=[('', 'Выберите назначение'), *CERTIFICATE_PURPOSES.items()]),
                'recipient': forms.CharField(label='Куда требуется справка', max_length=160,
                    widget=forms.TextInput(attrs={**attrs, 'autocomplete': 'off'})),
                'copies': forms.IntegerField(label='Количество экземпляров', min_value=1, max_value=10, initial=1),
            }
        else:
            fields = {
                'correction_area': forms.ChoiceField(label='Какие сведения', choices=[('', 'Выберите сведения'), *CORRECTION_AREAS.items()]),
                'record_label': forms.CharField(label='Дисциплина или поле', max_length=160,
                    widget=forms.TextInput(attrs={**attrs, 'autocomplete': 'off'})),
                'requested_change': forms.CharField(label='Что нужно проверить и исправить', max_length=500,
                    widget=forms.Textarea(attrs={**attrs, 'rows': 3})),
            }
        self.fields = {**fields, **self.fields}
        for name, field in self.fields.items():
            field.widget.attrs['data-sensitive-draft'] = ''


class DecisionForm(forms.Form):
    explanation = forms.CharField(required=False, strip=False,
        widget=forms.Textarea(attrs={'rows': 4, 'data-sensitive-draft': ''}))
