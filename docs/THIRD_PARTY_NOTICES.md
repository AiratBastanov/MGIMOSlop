# Сторонние компоненты и лицензии

[Права на оригинальные материалы](RIGHTS.md) · [Описание продукта](../README.md)

Точные Python-версии закреплены в [runtime](../requirements/runtime.txt), [production](../requirements/production.txt), [development](../requirements/development.txt) и [инструментах PDF](../requirements/pdf-tools.txt). Frontend — в [package-lock.json](../frontend/package-lock.json). Ниже приведены сохранённые уведомления из поставок; названия авторов и условия внутри них не переписывались.

| Компонент | Назначение и лицензия |
| --- | --- |
| Python 3.14.10 | Интерпретатор; [PSF и включённые компоненты](licenses/Python-3.14.10/LICENSE.txt) |
| Django 5.2.19, asgiref | Сервер приложения; [Django BSD](licenses/Django/LICENSE), [asgiref BSD](licenses/asgiref/LICENSE) |
| PostgreSQL 17.11 | Хранилище; [лицензия сервера](licenses/PostgreSQL-17.11/server_license.txt), [компоненты утилит](licenses/PostgreSQL-17.11/commandlinetools_3rd_party_licenses.txt) |
| psycopg и psycopg-binary 3.3.8 | Драйвер БД; [LGPL](licenses/psycopg/LICENSE.txt), [уведомления binary-пакета](licenses/psycopg-binary/LICENSE.txt) |
| Django Axes 9.0.0 | Ограничение попыток входа; [сохранённые уведомления](licenses/django-axes/LICENSE) |
| Pillow 13.0.0 | Проверка и перекодирование изображений; [MIT-CMU](licenses/pillow-13.0.0/LICENSE) |
| pypdf 7.0.0 | Ограниченная проверка PDF-вложений и инструменты документа; [BSD-3-Clause](licenses/pypdf-7.0.0/pypdf-7.0.0.dist-info/licenses/LICENSE) |
| React и React DOM 19.3.1, Scheduler | Компоненты интерфейса; [React MIT](licenses/task11/react/LICENSE), [React DOM MIT](licenses/task11/react-dom/LICENSE), [Scheduler](licenses/task11/scheduler/LICENSE) |
| TypeScript 5.9.3, Vite 7.3.7 | Проверка и сборка; [Apache-2.0](licenses/task11/typescript/LICENSE.txt), [Vite MIT и bundled notices](licenses/task11/vite/LICENSE.md) |
| Golos Text | Локальные шрифты; [SIL Open Font License](licenses/Golos-Text/OFL.txt) |
| Gunicorn 26.2.0, Caddy 2.11.4 | Схема Linux; [MIT](licenses/gunicorn/LICENSE), [Apache-2.0](licenses/Caddy/LICENSE) |
| Playwright 1.64.1 | Браузерные проверки и экспорт PDF; [Apache-2.0](licenses/playwright/LICENSE), [драйвер и компоненты](licenses/playwright-driver/LICENSE) |
| pypdfium2 5.15.1 / PDFium | Проверка отрисовки PDF; [полный комплект уведомлений](licenses/pypdfium2-5.15.1/) |

Транзитивные frontend-пакеты, версии и заявленные лицензии перечислены в [реестре зависимостей](licenses/task11/dependencies.json); тексты Rollup, esbuild, declarations и остальных пакетов сохранены рядом. Python-вспомогательные пакеты и их уведомления находятся в [каталоге лицензий](licenses/), включая sqlparse, tzdata, greenlet, pyee, typing_extensions, fonttools и Brotli.

Виртуальные среды, бинарники Python/PostgreSQL/браузеров, `node_modules`, модели и веса не распространяются в этом дереве. Сборка содержит собственные assets, лицензированные Golos и необходимые уведомления библиотек. Генератор изображений, сторонняя тема интерфейса и официальный логотип вуза не использовались. Геометрические аватары и синтетические тестовые файлы создаются кодом проекта.

Ollama/Qwen — отдельно устанавливаемая необязательная возможность, не включённая в Git. Её поставка и лицензия модели проверяются установщиком при выбранной загрузке. Прочитанные внешние материалы по дизайну и промптингу служили справочными источниками, не выдаются за лицензированный код или исходные изображения проекта.

Демонстрационный MP4 собран установленным FFmpeg 8.0 с libx264 и libass. Это внешний инструмент записи и монтажа; его бинарники не включены в репозиторий. В ролике используются собственный интерфейс, вымышленные данные и русские подписи; музыка и сторонние видеоматериалы не добавлялись.
