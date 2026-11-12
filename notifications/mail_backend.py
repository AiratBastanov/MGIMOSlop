"""Private local capture; no connection to an SMTP server."""
from pathlib import Path
import os
import uuid
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class CaptureBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        root = Path(settings.EMAIL_FILE_PATH)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        count = 0
        for message in email_messages:
            path = root / (uuid.uuid4().hex + '.eml')
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as file:
                file.write(message.message().as_bytes())
            count += 1
        return count
