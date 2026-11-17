"""Optional machine-local teacher settings; never load shell code or credentials."""
import json
import os
from pathlib import Path

FIELDS = {
    'backend': 'COURSE_TEACHER_BACKEND',
    'command': 'COURSE_TEACHER_COMMAND',
    'model': 'COURSE_TEACHER_MODEL',
}


def load_local_settings(root):
    path = Path(root) / '.local/teacher.json'
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Не удалось прочитать .local/teacher.json.') from None
    if not isinstance(data, dict) or set(data) - FIELDS.keys():
        raise ValueError('В .local/teacher.json разрешены только backend, command и model.')
    if any(not isinstance(value, str) or not value.strip() or '\x00' in value for value in data.values()):
        raise ValueError('Настройки .local/teacher.json должны быть непустыми строками.')
    if 'backend' in data and data['backend'] not in {'codex', 'command', 'api'}:
        raise ValueError('Неизвестное подключение в .local/teacher.json.')
    # A file setting wins over an inherited setting from an older shell.
    os.environ.update({FIELDS[key]: value for key, value in data.items()})
