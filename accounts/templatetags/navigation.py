from django import template
from accounts.navigation import navigation

register = template.Library()
register.simple_tag(navigation)
