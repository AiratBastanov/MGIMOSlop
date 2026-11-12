import json
from pathlib import Path
from .contracts import validate, ImportRejected, SourceUnavailable

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportRejected('duplicate_json_key','Повтор ключа JSON')
        result[key] = value
    return result

class SyntheticDatasetProvider:
    def load(self, path):
        try:
            with Path(path).open('rb') as file:
                raw = file.read(2 * 1024 * 1024 + 1)
        except OSError as exc:
            raise SourceUnavailable('source_unavailable','Файл источника недоступен') from exc
        if len(raw) > 2 * 1024 * 1024:
            raise ImportRejected('size','Файл превышает 2 МиБ')
        try:
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                                 parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            return validate(payload)
        except (UnicodeError, ValueError, RecursionError, TypeError) as exc:
            raise ImportRejected('json','Некорректный UTF-8 JSON или тип значения') from exc
