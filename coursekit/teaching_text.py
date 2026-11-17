"""Idempotent presentation accents for teacher prose; never rewrite model data."""
import re

PROTECTED = re.compile(r'(`+[^`\n]*`+|\[[^\]\n]*\]\([^\n)]*\)|https?://\S+)')
KEYS = re.compile(r'(?<![\w`])(?:Ctrl\+[A-Za-z]|Shift\+Tab|PgUp|PgDn|Backspace|Delete|Enter|Esc|Tab|Home|End|F(?:1[0-2]|[1-9]))(?![\w`])')
FIELDS = re.compile(r'(?<!\w)(Вход модели|Выход модели)(?!\w)')


def teacher_markdown(text):
    lines=[]
    fence=None
    for line in text.splitlines(keepends=True):
        marker=re.match(r'^\s*(`{3,}|~{3,})',line)
        if marker:
            value=marker[1]
            if fence is None:fence=value
            elif value[0]==fence[0] and len(value)>=len(fence):fence=None
            lines.append(line)
            continue
        if fence:
            lines.append(line)
            continue
        parts=PROTECTED.split(line)
        for i in range(0,len(parts),2):
            part=KEYS.sub(lambda m:'`'+m[0]+'`',parts[i])
            parts[i]=FIELDS.sub(lambda m:m[0] if part[max(0,m.start()-2):m.start()]=='**' else '**'+m[0]+'**',part)
        lines.append(''.join(parts))
    return ''.join(lines)
