from .base import *  # noqa: F403

if setting('DB_NAME') != 'deanery_test' or setting('DB_USER') != 'deanery_test':  # noqa: F405
    raise RuntimeError('Тесты разрешены только в отдельной deanery_test под ролью deanery_test')
TEST_RUNNER = 'deanery.test_runner.ExistingDatabaseRunner'
