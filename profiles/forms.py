from io import BytesIO
from urllib.parse import urlsplit
import warnings
import re
from django import forms
from django.core.exceptions import ValidationError
from PIL import Image, ImageOps, UnidentifiedImageError
from .models import Preferences, GroupResource


def safe_image(upload):
    if upload.size > 2 * 1024 * 1024:
        raise ValidationError('Фото должно быть не больше 2 МБ.')
    raw = upload.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValidationError('Фото должно быть не больше 2 МБ.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as check:
                if check.format not in ('JPEG', 'PNG', 'WEBP') or getattr(check, 'n_frames', 1) != 1:
                    raise ValueError()
                if not 32 <= check.width <= 4096 or not 32 <= check.height <= 4096 or check.width * check.height > 12_000_000:
                    raise ValueError()
                check.verify()
            with Image.open(BytesIO(raw)) as decoded:
                decoded.load()
                oriented = ImageOps.exif_transpose(decoded)
                oriented.thumbnail((512, 512))
                rgb = Image.new('RGB', oriented.size, 'white')
                if oriented.mode in ('RGBA', 'LA') or 'transparency' in oriented.info:
                    rgba = oriented.convert('RGBA')
                    rgb.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    rgb.paste(oriented.convert('RGB'))
                output = BytesIO()
                rgb.save(output, format='JPEG', quality=85, optimize=True)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError('Выберите статичное JPEG, PNG или WebP: от 32 до 4096 пикселей, не более 12 млн пикселей.')


class ProfileForm(forms.ModelForm):
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput())
    photo = forms.FileField(label='Новое фото', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': 'image/jpeg,image/png,image/webp'}))
    remove_photo = forms.BooleanField(label='Удалить текущее фото', required=False)

    class Meta:
        model = Preferences
        fields = ['contact_email', 'email_enabled', 'email_requests', 'email_academic', 'email_announcements', 'photo_audience', 'share_email', 'directory_phone', 'share_phone']
        labels = {'contact_email': 'Адрес для уведомлений', 'email_enabled': 'Получать уведомления по почте',
            'email_requests': 'Заявки', 'email_academic': 'Учёба и расписание', 'email_announcements': 'Объявления',
            'photo_audience': 'Кто видит моё фото', 'directory_phone': 'Телефон для группы · необязательно'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['data-sensitive-draft'] = ''

    def clean_contact_email(self):
        value = self.cleaned_data['contact_email'].strip()
        if value:
            local, domain = value.rsplit('@', 1)
            value = local + '@' + domain.lower()
        return value

    def clean_photo(self):
        upload = self.cleaned_data.get('photo')
        return safe_image(upload) if upload else None

    def clean_directory_phone(self):
        raw=self.cleaned_data.get('directory_phone','').strip()
        if not raw: return ''
        if not re.fullmatch(r'\+?[0-9 ()-]{7,24}',raw):
            raise ValidationError('Введите телефон: от 7 до 15 цифр, можно с +, пробелами и скобками.')
        digits=re.sub(r'\D','',raw)
        if not 7<=len(digits)<=15: raise ValidationError('В телефоне должно быть от 7 до 15 цифр.')
        return ('+' if raw.startswith('+') else '')+digits

    def clean(self):
        data = super().clean()
        if data.get('email_enabled') and not data.get('contact_email'):
            self.add_error('contact_email', 'Укажите адрес для писем.')
        if data.get('photo') and data.get('remove_photo'):
            self.add_error('photo', 'Выберите замену или удаление фото.')
        if data.get('share_email') and (not self.instance.email_verified_at or data.get('contact_email')!=self.instance.contact_email):
            self.add_error('share_email','Сначала сохраните и подтвердите адрес, затем разрешите его показ группе.')
        if data.get('share_phone') and not data.get('directory_phone'):
            self.add_error('directory_phone','Укажите телефон или отключите его показ.')
        return data


def https_link(value):
    if not value:
        return ''
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValidationError('Укажите HTTPS-ссылку без логина и пароля.')
    return value


class ResourceForm(forms.ModelForm):
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput())

    class Meta:
        model = GroupResource
        fields = ['group', 'category', 'title', 'description', 'url', 'available_from', 'available_until']
        labels = {'group': 'Группа', 'category': 'Раздел', 'title': 'Заголовок', 'description': 'Описание',
            'url': 'HTTPS-ссылка (необязательно)', 'available_from': 'Показывать с (МСК)', 'available_until': 'Показывать до (МСК)'}
        widgets = {'available_from': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
            'available_until': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
            'description': forms.Textarea(attrs={'rows': 4})}

    def __init__(self, *args, groups, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['group'].queryset = groups
        self.fields['group'].label_from_instance = lambda obj: obj.name
        for field in self.fields.values():
            field.widget.attrs['data-sensitive-draft'] = ''

    def clean_url(self):
        try:
            return https_link(self.cleaned_data['url'])
        except ValueError:
            raise ValidationError('Проверьте HTTPS-ссылку.')
