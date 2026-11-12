"""Visual tone only; official result semantics stay in academic_selectors."""
from django import template

register = template.Library()


@register.filter
def result_tone(result):
    if not result:
        return 'missing'
    if result['kind'] == 'grade' and result['grade'] == 3:
        return 'satisfactory'
    return 'positive' if result['positive'] else 'negative'
