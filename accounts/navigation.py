"""Visible destinations shared by the shell and the verified search catalogue."""

def destination(key, label, url, *sections, mobile=None, keywords=''):
    return dict(key=key, label=label, url=url, sections=sections or (key,),
                mobile_label=mobile or label, keywords=keywords)


PRIMARY = {
    'student': (
        destination('today', 'Сегодня', '/today/'),
        destination('schedule', 'Расписание', '/schedule/', 'schedule', 'lesson', keywords='расписание занятий пары'),
        destination('study', 'Учёба', '/study/', 'study', 'enrollment', 'assessment', 'materials'),
        destination('requests', 'Заявки', '/requests/', 'requests', 'request_new', 'request_detail'),
        destination('notifications', 'Уведомления', '/notifications/', 'notifications', 'announcement', mobile='Входящие', keywords='входящие история уведомления'),
        destination('group', 'Моя группа', '/profile/group/'),
    ),
    'teacher': (
        destination('overview', 'Обзор', '/teacher/', 'teacher_home'),
        destination('courses', 'Дисциплины', '/teacher/courses/', 'teacher_courses', 'teacher', keywords='курс дисциплина задания материал'),
        destination('schedule', 'Расписание', '/teacher/schedule/', 'teacher_schedule', keywords='расписание занятий пары'),
        destination('review', 'Проверка', '/teacher/review/', 'teacher_review', keywords='проверить отправленные работы отзыв'),
        destination('notifications', 'Входящие', '/notifications/', 'notifications', keywords='уведомления история'),
    ),
    'staff': (
        destination('overview', 'Обзор', '/staff/', 'staff_home'),
        destination('requests', 'Заявки', '/staff/requests/', 'staff_requests', 'staff_request_detail', keywords='справка заявление обмен план заявки факультета'),
        destination('notices', 'Объявления', '/staff/notices/', 'staff_notices', keywords='объявление новости службы'),
        destination('groups', 'Группы', '/staff/groups/', 'staff_groups', 'resources'),
        destination('notifications', 'Входящие', '/notifications/', 'notifications', keywords='уведомления история'),
    ),
}
PROFILE = (
    destination('profile', 'Мой профиль', '/profile/'),
    destination('settings', 'Настройки профиля', '/profile/edit/'),
)


def navigation(role):
    primary = PRIMARY.get(role, ())
    return {'primary': primary, 'mobile': primary[:5],
            'profile': PROFILE + ((primary[-1],) if role == 'student' else ()) if primary else ()}
