from django import forms
from .selectors import MODE_LABELS, REVIEW_LABELS, CLOUD_HOSTS

SENSITIVE={'data-sensitive-draft':''}


class DraftForm(forms.Form):
    assignment=forms.ChoiceField(label='Преподаватель и дисциплина',widget=forms.Select(attrs=SENSITIVE))
    title=forms.CharField(label='Название',max_length=160,widget=forms.TextInput(attrs=SENSITIVE))
    mode=forms.ChoiceField(label='Формат',choices=list(MODE_LABELS.items()),widget=forms.Select(attrs=SENSITIVE))
    content=forms.CharField(label='Содержание',required=False,strip=False,
        widget=forms.Textarea(attrs={**SENSITIVE,'rows':6}))

    def __init__(self,*args,assignments=(),editing=False,policy=None,**kwargs):
        super().__init__(*args,**kwargs)
        selected=self.data.get('assignment') if self.is_bound else self.initial.get('assignment')
        if policy is None:
            policy=next((row['policy'] for row in assignments if str(row['id'])==str(selected)),None)
        self.policy=policy
        if editing:
            self.fields.pop('assignment')
        else:
            self.fields['assignment'].choices=[('', 'Выберите назначение')] + [(str(row['id']),f"{row['course']} · {row['teacher']}") for row in assignments]
        modes=policy['modes'] if policy else []
        self.fields['mode'].choices=[(mode,MODE_LABELS[mode]) for mode in modes]
        selected_mode=self.data.get('mode') if self.is_bound else self.initial.get('mode')
        mode=selected_mode or (modes[0] if modes else 'text')
        if not selected_mode and modes:
            self.initial['mode']=mode
        content=self.fields['content']
        if mode=='link':
            content.label='Ссылка на облако'
            content.widget=forms.URLInput(attrs={**SENSITIVE,'maxlength':1500,'inputmode':'url'})
            content.help_text='HTTPS-ссылка. Разрешены: '+', '.join(policy['cloud_hosts'] if policy else [])+'. Содержимое ссылки не загружается в кабинет.'
        elif mode=='email':
            content.label='Контактная почта'
            content.widget=forms.EmailInput(attrs={**SENSITIVE,'maxlength':254,'autocomplete':'email'})
            content.help_text='Адрес для передачи материала преподавателю, не более 254 символов.'
        else:
            limit=policy['text_limit'] if policy else 20000
            content.widget=forms.Textarea(attrs={**SENSITIVE,'rows':6,'maxlength':limit})
            content.help_text=f'До {limit} символов. Условия проверяются снова при отправке.' if policy else 'Сначала выберите преподавателя и дисциплину.'


class PolicyForm(forms.Form):
    modes=forms.MultipleChoiceField(label='Разрешённые форматы',choices=list(MODE_LABELS.items()),
        widget=forms.CheckboxSelectMultiple(attrs=SENSITIVE))
    text_limit=forms.IntegerField(label='Максимум символов текста',min_value=100,max_value=20000,
        widget=forms.NumberInput(attrs=SENSITIVE))
    cloud_hosts=forms.MultipleChoiceField(label='Разрешённые облачные сервисы',required=False,
        choices=[(host,host) for host in CLOUD_HOSTS],widget=forms.CheckboxSelectMultiple(attrs=SENSITIVE))
    instructions=forms.CharField(label='Краткие условия',required=False,max_length=500,
        widget=forms.Textarea(attrs={**SENSITIVE,'rows':3}))
    attachments_allowed=forms.BooleanField(label='Разрешить вложения',required=False,widget=forms.CheckboxInput(attrs=SENSITIVE))
    attachment_types=forms.MultipleChoiceField(label='Типы вложений',required=False,
        choices=[('pdf','PDF'),('docx','DOCX'),('txt','TXT (UTF-8)'),('jpeg','JPEG'),('png','PNG'),('webp','WebP без анимации')],widget=forms.CheckboxSelectMultiple(attrs=SENSITIVE))
    attachment_count=forms.IntegerField(label='Число файлов (до 5)',initial=5,min_value=1,max_value=5,widget=forms.NumberInput(attrs=SENSITIVE))
    attachment_file_mib=forms.IntegerField(label='Размер одного файла, МиБ (до 10)',initial=10,min_value=1,max_value=10,widget=forms.NumberInput(attrs=SENSITIVE))
    attachment_total_mib=forms.IntegerField(label='Общий размер, МиБ (до 25)',initial=25,min_value=1,max_value=25,widget=forms.NumberInput(attrs=SENSITIVE))


class ReviewForm(forms.Form):
    status=forms.ChoiceField(label='Результат проверки',choices=list(REVIEW_LABELS.items()),widget=forms.Select(attrs=SENSITIVE))
    feedback=forms.CharField(label='Отзыв к этой версии',max_length=2000,
        widget=forms.Textarea(attrs={**SENSITIVE,'rows':4}))
