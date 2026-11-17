"""Single-line terminal editor that survives asynchronous classroom output."""
import codecs
import os
import select
import sys
import termios
import tty
from rich.cells import cell_len


class TerminalInput:
    def __init__(self, stream, *, poll, status):
        self.stream = stream
        self.poll = poll
        self.status = status
        self.active = False
        self.text = ''
        self.position = 0
        self.buffer = ''
        self.last_render = None
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def clear(self):
        if self.active:
            print('\r\x1b[2K', end='', flush=True)
            self.last_render = None

    def draw(self):
        if not self.active:
            return
        columns = os.get_terminal_size(sys.stdout.fileno()).columns
        prefix = self.status() + 'Вы > '
        # A long draft scrolls horizontally instead of wrapping into the dialogue.
        available = max(1, columns - cell_len(prefix) - 2)
        start = self.position
        while start > 0 and cell_len(self.text[start-1:self.position]) <= available:
            start -= 1
        shown = self.text[start:]
        while cell_len(shown) > available:
            shown = shown[:-1]
        caret = cell_len(prefix + self.text[start:self.position])
        rendered = (prefix + shown, caret, columns)
        if rendered != self.last_render:
            print('\r\x1b[2K' + prefix + shown + '\r' + (f'\x1b[{caret}C' if caret else ''), end='', flush=True)
            self.last_render = rendered

    def read(self):
        fd = self.stream.fileno()
        previous = termios.tcgetattr(fd)
        self.text, self.position = '', 0
        self.active = True
        escape = ''
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            while True:
                self.poll()
                self.draw()
                if not self.buffer:
                    if not select.select([fd], [], [], .1)[0]:
                        continue
                    data = os.read(fd, 4096)
                    if not data:
                        return ''
                    self.buffer += self.decoder.decode(data)
                while self.buffer:
                    character, self.buffer = self.buffer[0], self.buffer[1:]
                    if escape or character == '\x1b':
                        escape += character
                        actions = {'\x1b[D': -1, '\x1b[C': 1, '\x1bOD': -1, '\x1bOC': 1}
                        if escape in actions:
                            self.position = max(0, min(len(self.text), self.position + actions[escape]))
                            escape = ''
                        elif escape in {'\x1b[H', '\x1bOH', '\x1b[1~'}:
                            self.position, escape = 0, ''
                        elif escape in {'\x1b[F', '\x1bOF', '\x1b[4~'}:
                            self.position, escape = len(self.text), ''
                        elif escape == '\x1b[3~':
                            self.text = self.text[:self.position] + self.text[self.position+1:]
                            escape = ''
                        elif len(escape) > 2 and (escape[-1].isalpha() or escape[-1] == '~'):
                            escape = ''  # Ignore function keys and bracketed-paste markers.
                        elif len(escape) > 12:
                            escape = ''
                        continue
                    if character in {'\n', '\r'}:
                        self.clear()
                        print(flush=True)
                        return self.text + '\n'
                    if character == '\x04' and not self.text:
                        return ''
                    if character in {'\x7f', '\x08'} and self.position:
                        self.text = self.text[:self.position-1] + self.text[self.position:]
                        self.position -= 1
                    elif character == '\x01':
                        self.position = 0
                    elif character == '\x05':
                        self.position = len(self.text)
                    elif character == '\x15':
                        self.text, self.position = self.text[self.position:], 0
                    elif character.isprintable():
                        if len(self.text) >= 8192:
                            continue
                        self.text = self.text[:self.position] + character + self.text[self.position:]
                        self.position += 1
        finally:
            self.clear()
            self.active = False
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
