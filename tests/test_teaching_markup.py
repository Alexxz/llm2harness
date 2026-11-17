"""Check real Rich output, including the boundary between prose and literal data."""
from contextlib import redirect_stdout
import io
import re
import unittest
from unittest.mock import patch

from rich.console import Console
from coursekit.classroom_tmux import board_say


class TeachingMarkupTests(unittest.TestCase):
    def render(self, text, label='ПРЕПОДАВАТЕЛЬ', color=True):
        original=Console
        def make_console(*args,**kwargs):
            kwargs.update(force_terminal=color, no_color=not color, color_system='standard' if color else None)
            return original(*args,**kwargs)
        output=io.StringIO()
        with redirect_stdout(output),patch('rich.console.Console',side_effect=make_console):
            board_say(text,label=label,columns=55)
        return output.getvalue()

    def test_hotkeys_and_field_names_have_distinct_visible_accents(self):
        value=self.render('В поле «**Вход модели**» расположен текст.\n\n**Попробуй**\n\nНажми `F3`, затем `F2`.')
        self.assertIn('\x1b[1;96mF3\x1b[0m',value)
        self.assertIn('\x1b[1;33mВход модели\x1b[0m',value)
        plain=re.sub(r'\x1b\[[0-9;]*m','',value)
        self.assertNotIn('**',plain)
        self.assertNotIn('`',plain)
        self.assertIn('Попробуй',plain)
        self.assertTrue(all(len(line)<=55 for line in plain.splitlines()))

    def test_without_color_the_instruction_and_names_are_still_readable(self):
        value=self.render('Найди «**Выход модели**» и нажми `Esc`.',color=False)
        self.assertNotIn('\x1b',value)
        self.assertIn('Выход модели',value)
        self.assertIn('Esc',value)
        self.assertNotIn('**',value)
        self.assertNotIn('`',value)

    def test_student_and_model_text_are_literal_and_controls_are_escaped(self):
        for label in ('ТВОЯ РЕПЛИКА','ОТВЕТ МОДЕЛИ'):
            value=self.render('**текст** `F3` [red] пример\x1b[31m',label=label,color=False)
            self.assertIn('**текст**',value)
            self.assertIn('`F3`',value)
            self.assertIn('[red]',value)
            self.assertNotIn('\x1b',value)

    def test_unformatted_generated_teacher_reply_gets_the_same_accents(self):
        value=self.render('Нажми F3, найди «Вход модели» и вернись через F2.')
        self.assertIn('\x1b[1;96mF3\x1b[0m',value)
        self.assertIn('\x1b[1;33mВход модели\x1b[0m',value)
        from coursekit.teaching_text import teacher_markdown
        text='Нажми `F3`, найди «**Вход модели**».\n```text\nF2 Вход модели\n```\n'
        self.assertEqual(teacher_markdown(text),text)
        self.assertEqual(teacher_markdown(teacher_markdown('F3 Вход модели')),teacher_markdown('F3 Вход модели'))

    def test_file_and_fallback_teacher_instructions_cannot_diverge(self):
        from pathlib import Path
        from coursekit.teacher import INSTRUCTION
        root=Path(__file__).resolve().parents[1]
        self.assertEqual((root/'course/TEACHER_PROMPT.md').read_text().strip(),INSTRUCTION.strip())
        self.assertIn('он не отменяет F1',INSTRUCTION)
