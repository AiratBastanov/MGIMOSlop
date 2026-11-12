from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from accounts.access import current_actor, lock_guard, AccessDenied
from accounts.models import Identity
from accounts.selectors import private_snapshot
from accounts.views import protected
from .forms import ProfileForm, ResourceForm
from .models import Avatar, GroupResource
from .selectors import own_preferences, can_see_person, group_directory, group_resources, publisher_groups
from .services import update_profile, verify_email, request_verification, save_resource


@require_http_methods(['GET', 'POST'])
@protected
def edit_profile(request):
    if request.method == 'POST':
        with transaction.atomic():
            actor = current_actor(request, guard=lock_guard(), lock=True, expected=request.POST.get('context', ''))
            form = ProfileForm(request.POST, request.FILES, instance=own_preferences(actor))
            if form.is_valid():
                update_profile(request, actor.marker, form.cleaned_data)
                return redirect('/profile/edit/?saved=1')
    data = private_snapshot(request)
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=data['marker'])
        if request.method == 'POST' and request.POST.get('context', '') != actor.marker:
            raise AccessDenied('stale_context', 409)
        pref = own_preferences(actor)
        if request.method == 'GET':
            form = ProfileForm(instance=pref, initial={'version': pref.version})
        data.update(form=form, preferences=pref, saved=request.GET.get('saved') == '1', section='profile')
        return render(request, 'profiles/edit.html', data, status=400 if form.is_bound and form.errors else 200)


@require_POST
@protected
def resend_verification(request):
    request_verification(request, request.POST.get('context', ''))
    return redirect('/profile/edit/?queued=1')


@require_http_methods(['GET', 'POST'])
@protected
def confirm_email(request):
    if request.method == 'POST':
        verify_email(request, request.POST.get('context', ''), request.POST.get('token', ''))
        return redirect('/profile/edit/?verified=1')
    data = private_snapshot(request)
    data['token'] = request.GET.get('token', '')[:1000]
    return render(request, 'profiles/verify.html', data)


@require_GET
@protected
def photo(request, identity_id):
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True)
        identity = Identity.objects.select_related('user', 'profile', 'instructor').filter(pk=identity_id).first()
        if not identity or not can_see_person(actor, identity):
            raise AccessDenied('not_found', 404)
        image = Avatar.objects.filter(identity=identity).first()
        if not image:
            raise AccessDenied('not_found', 404)
        response = HttpResponse(bytes(image.image), content_type='image/jpeg')
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Disposition'] = 'inline; filename="avatar.jpg"'
        return response


@require_GET
@protected
def my_group(request):
    data = private_snapshot(request)
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=data['marker'])
        data.update(section='group', classmates=group_directory(actor), resources=list(group_resources(actor, actor.checked_at)))
        return render(request, 'profiles/group.html', data)


@require_http_methods(['GET', 'POST'])
@protected
def resources(request, resource_id=None):
    data = private_snapshot(request)
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True,
            expected=request.POST.get('context', '') if request.method == 'POST' else data['marker'])
        groups = publisher_groups(actor)
        resource = GroupResource.objects.filter(pk=resource_id, group__in=groups).first() if resource_id else None
        if resource_id and resource is None:
            raise AccessDenied('not_found', 404)
        form = ResourceForm(request.POST if request.method == 'POST' else None, instance=resource,
            groups=groups, initial={'version': resource.version if resource else 0})
        if request.method == 'POST' and form.is_valid():
            pk = save_resource(request, actor.marker, form.cleaned_data, resource_id)
            return redirect(f'/staff/resources/{pk}/?saved=1')
        data.update(form=form, resource=resource, resources=list(GroupResource.objects.filter(group__in=groups)
            .select_related('group').order_by('-updated_at', '-pk')), section='resources', saved=request.GET.get('saved') == '1')
        return render(request, 'profiles/resources.html', data, status=400 if form.is_bound and form.errors else 200)
