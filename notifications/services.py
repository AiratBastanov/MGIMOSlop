from .models import Notification

def record_schedule_change(change, *, published_at):
    return Notification.objects.create(schedule_change=change, group=change.lesson.group,
                                       published_at=published_at)


def record_request_event(event):
    """Обязательная запись внутри транзакции заявки, без отдельного commit.

    OneToOne не допускает второе уведомление, immutable trigger сохраняет
    историческую идентичность и публикацию при последующих переходах заявки.
    """
    return Notification.objects.create(request_event=event, published_at=event.recorded_at)
