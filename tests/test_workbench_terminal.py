"""Verify the actual curses screen inside an isolated tmux, without a lesson or model."""
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from coursekit.classroom import Session
from coursekit.classroom_tmux import TmuxServer, child_argv
from coursekit.workbench import Workspace, advance_phase, teacher_view

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required for terminal checks')
class WorkspaceTerminalTests(unittest.TestCase):
    def wait(self, server, pane, predicate):
        deadline = time.monotonic()+8
        last = ''
        while time.monotonic() < deadline:
            last = server.command('capture-pane', '-p', '-t', pane).stdout
            if predicate(last):
                return last
            time.sleep(.05)
        self.fail('Unexpected terminal screen:\n' + last)

    def assert_fields(self, screen):
        self.assertIn('Вход модели', screen)
        self.assertIn('Выход модели', screen)
        self.assertIn('max_tokens:', screen)
        self.assertIn('temperature:', screen)
        self.assertIn('seed:', screen)
        lines = screen.splitlines()
        for label in ('Вход модели', 'Выход модели'):
            top = next(i for i, line in enumerate(lines) if label in line and '┌' in line)
            self.assertTrue(lines[top].rstrip().endswith('┐'))
            bottom = next(i for i in range(top+1, len(lines)) if lines[i].startswith('└'))
            self.assertTrue(lines[bottom].rstrip().endswith('┘'))
            for line in lines[top+1:bottom]:
                self.assertTrue(line.startswith('│'), line)
                self.assertTrue(line.rstrip().endswith('│'), line)

    def test_presets_keep_both_framed_fields_visible_and_do_not_execute(self):
        for columns, rows in ((46,22), (80,44)):
            for lesson in ('00','01','04'):
                with self.subTest(size=(columns,rows), lesson=lesson), tempfile.TemporaryDirectory(prefix='workspace-ui-', dir='/tmp') as temporary:
                    root = Path(temporary)
                    source = next((ROOT/'lessons').glob(lesson+'-*'))
                    shutil.copytree(source, root/'lessons'/source.name, ignore=shutil.ignore_patterns('.env','.env.*','.idea'))
                    session = Session.create(lesson, root=root)
                    lab = Workspace(session)
                    if lesson == '01':
                        advance_phase(session)
                    server = TmuxServer(shutil.which('tmux'), root/'socket')
                    server.start()
                    try:
                        pane = server.command('new-session', '-d', '-s', 'course', '-x', str(columns), '-y', str(rows+1), '-P', '-F', '#{pane_id}', '--', *child_argv(session,'experiment')).stdout.strip()
                        # Reserve exactly rows lines for curses, with no attached teacher process.
                        server.command('set-option', '-g', 'status', 'on')
                        text = self.wait(server,pane,lambda s: 'Выход модели' in s)
                        self.assert_fields(text)
                        self.assertNotIn('ПРЕСЕТЫ',text)
                        self.assertNotIn('Запросов ещё не было',text)
                        lines=text.splitlines()
                        output_top=next(i for i,line in enumerate(lines) if 'Выход модели' in line)
                        output_bottom=next(i for i in range(output_top+1,len(lines)) if lines[i].startswith('└'))
                        self.assertTrue(all(not line.strip('│ ') for line in lines[output_top+1:output_bottom]))
                        original = teacher_view(session)['prompt']
                        server.command('send-keys','-t',pane,'F4')
                        text = self.wait(server,pane,lambda s: 'ПРЕСЕТЫ' in s)
                        self.assert_fields(text)
                        self.assertIn('[baseline]',text)
                        self.assertEqual(teacher_view(session)['interface']['panel'],'presets')
                        if lesson == '01':
                            server.command('send-keys','-t',pane,'Down')
                            self.wait(server,pane,lambda s: '› [variation]' in s)
                            self.assertEqual(teacher_view(session)['prompt'],original)
                            server.command('send-keys','-t',pane,'Escape')
                            self.wait(server,pane,lambda s: 'ПРЕСЕТЫ' not in s)
                            self.assertEqual(teacher_view(session)['prompt'],original)
                            server.command('send-keys','-t',pane,'F4','Down')
                            self.wait(server,pane,lambda s: '› [variation]' in s)
                            preset = 'variation'
                        else:
                            preset = 'baseline'
                        server.command('send-keys','-t',pane,'Enter')
                        self.wait(server,pane,lambda s: 'ПРЕСЕТЫ' not in s and teacher_view(session)['preset'] == preset)
                        state = teacher_view(session)
                        self.assertEqual(state['preset'],preset)
                        self.assertEqual(state['interface']['panel'],'workspace')
                        self.assertEqual(state['output'],'')
                        self.assertIsNone(session.snapshot()['last_run'])
                        self.assertFalse(any(e['event']=='run_started' for e in session.events(limit=100)))
                        session.control('close')
                    finally:
                        server.stop()

    def test_real_navigation_headers_and_teaching_question_order(self):
        from coursekit.lesson_flow import progress
        from coursekit.teacher import TeacherRuntime
        from coursekit.classroom_tmux import configure_interface
        with tempfile.TemporaryDirectory(prefix='workspace-flow-', dir='/tmp') as temporary:
            root=Path(temporary)
            source=ROOT/'lessons/00-interface'
            shutil.copytree(source, root/'lessons'/source.name)
            session=Session.create('00',root=root)
            Workspace(session)
            class Backend:
                def answer(self,events):
                    return {'reply':'Обсудили наблюдение.','note':'synthetic teacher assessment','control':'advance'}
                def close(self):pass
            server=TmuxServer(shutil.which('tmux'),root/'socket')
            server.start()
            runtime=TeacherRuntime(session,Backend())
            def until(predicate):
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.05)
                self.fail(str(progress(session)))
            try:
                teacher=server.command('new-session','-d','-s','course','-x','160','-y','44','-P','-F','#{pane_id}','--','/bin/cat').stdout.strip()
                pane=server.command('split-window','-h','-t',teacher,'-P','-F','#{pane_id}','--',*child_argv(session,'experiment')).stdout.strip()
                configure_interface(server,session,teacher,pane)
                self.wait(server,pane,lambda s:'Выход модели' in s)
                self.assertEqual(server.command('display-message','-p','-t',teacher,'#{pane_title}').stdout.strip(),'ПРЕПОДАВАТЕЛЬ · F2')
                self.assertEqual(server.command('display-message','-p','-t',pane,'#{pane_title}').stdout.strip(),'ЛАБОРАТОРИЯ · F3')
                runtime.start()
                until(lambda:progress(session)['introduced'])
                server.command('send-keys','-t',pane,'F3','F1')
                self.wait(server,pane,lambda s:'Предыдущие опыты' in s)
                server.command('send-keys','-t',pane,'Escape','F2')
                until(lambda:progress(session)['question_asked'])
                session.ask('Справа меняю текст для модели, слева обсуждаю с преподавателем.')
                until(lambda:progress(session)['phase_index']==1 and progress(session)['introduced'])
                self.assertFalse(progress(session)['experience_completed'])
                self.assertFalse(progress(session)['question_asked'])
                server.command('send-keys','-t',pane,'F3','F1')
                self.wait(server,pane,lambda s:'Предыдущие опыты' in s)
                server.command('send-keys','-t',pane,'Escape','F2')
                until(lambda:progress(session)['question_asked'])
                self.assertFalse(progress(session)['student_replied'])
                self.assertIsNone(session.snapshot()['last_run'])
                self.assertEqual(session.snapshot()['gate'],'paused')
                session.control('close')
            finally:
                runtime.close()
                server.stop()

    def test_client_hotkeys_are_not_executed_during_binding_and_are_journalled(self):
        import fcntl
        import os
        import pty
        import struct
        import subprocess
        import termios
        from coursekit.classroom_tmux import configure_interface
        with tempfile.TemporaryDirectory(prefix='workspace-client-keys-',dir='/tmp') as temporary:
            root=Path(temporary)
            source=ROOT/'lessons/00-interface'
            shutil.copytree(source,root/'lessons'/source.name)
            session=Session.create('00',root=root)
            Workspace(session)
            server=TmuxServer(shutil.which('tmux'),root/'socket')
            server.start()
            client=None
            master=None
            def until(predicate):
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if master is not None:
                        try:
                            while os.read(master,65536):pass
                        except (BlockingIOError,OSError):pass
                    if predicate():return
                    time.sleep(.05)
                self.fail('Actual client hotkey was not applied')
            def observed(action):
                return any(e['event']=='workspace_state' and e['data']['action']==action for e in session.events(limit=100))
            try:
                teacher=server.command('new-session','-d','-s','course','-x','160','-y','44','-P','-F','#{pane_id}','--','/bin/cat').stdout.strip()
                pane=server.command('split-window','-h','-t',teacher,'-P','-F','#{pane_id}','--',*child_argv(session,'experiment')).stdout.strip()
                self.wait(server,pane,lambda s:'Выход модели' in s)
                configure_interface(server,session,teacher,pane)
                time.sleep(.25)
                self.assertFalse(observed('focused'))
                self.assertFalse(observed('help_opened'))
                self.assertEqual(teacher_view(session)['interface']['panel'],'workspace')
                master,slave=pty.openpty()
                os.set_blocking(master,False)
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',44,160,0,0))
                client=subprocess.Popen([*server.prefix,'attach-session','-t','course'],stdin=slave,stdout=slave,stderr=slave,env={**server.environment,'TERM':'xterm-256color'})
                os.close(slave)
                until(lambda:bool(server.command('list-clients').stdout.strip()))
                os.write(master,b'\x1bOR')  # F3 through the attached client's root key table.
                until(lambda:observed('focused'))
                self.assertIn(pane+' 1',server.command('list-panes','-F','#{pane_id} #{pane_active}').stdout)
                os.write(master,b'\x1bOQ')  # F2, with a separate journal observation.
                until(lambda:observed('teacher_focus'))
                until(lambda:teacher+' 1' in server.command('list-panes','-F','#{pane_id} #{pane_active}').stdout)
                os.write(master,b'\x1bOS')  # F4 must reach the UI only now, never during bind-key.
                until(lambda:teacher_view(session)['interface']['panel']=='presets')
                self.assertIsNone(session.snapshot()['last_run'])
                server.command('send-keys','-t',pane,'Escape')
                until(lambda:teacher_view(session)['interface']['panel']=='workspace')
                os.write(master,b'\x15My draft\x1bOS')
                until(lambda:teacher_view(session)['interface']['panel']=='presets')
                self.assertEqual(teacher_view(session)['prompt'],'My draft')
                self.assertTrue(observed('edited'))
                os.write(master,b'\r')
                until(lambda:teacher_view(session)['preset']=='baseline')
                self.assertNotEqual(teacher_view(session)['prompt'],'My draft')
                self.assertEqual(teacher_view(session)['output'],'')
                session.control('close')
            finally:
                server.stop()
                if client is not None:
                    try:client.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        client.terminate()
                        client.wait(timeout=3)
                if master is not None:os.close(master)

    def test_edit_cursor_is_inside_input_after_footer_and_moves_with_cyrillic(self):
        for columns,rows in ((46,22),(80,44)):
            with self.subTest(size=(columns,rows)),tempfile.TemporaryDirectory(prefix='workspace-cursor-',dir='/tmp') as temporary:
                root=Path(temporary);source=ROOT/'lessons/03-structured-prompts'
                shutil.copytree(source,root/'lessons'/source.name)
                session=Session.create('03',root=root)
                server=TmuxServer(shutil.which('tmux'),root/'socket');server.start()
                try:
                    pane=server.command('new-session','-d','-s','course','-x',str(columns),'-y',str(rows+1),'-P','-F','#{pane_id}','--',*child_argv(session,'experiment')).stdout.strip()
                    self.wait(server,pane,lambda s:'редактирование' in s)
                    def cursor():
                        return server.command('display-message','-p','-t',pane,'#{cursor_x} #{cursor_y} #{cursor_flag}').stdout.strip()
                    self.assertEqual(cursor(),'2 5 1')
                    server.command('send-keys','-t',pane,'Right')
                    server.command('send-keys','-t',pane,'-l','Я')
                    self.wait(server,pane,lambda s:'ПЯреобразуй' in s)
                    self.assertEqual(cursor(),'4 5 1')
                    server.command('send-keys','-t',pane,'BSpace')
                    self.wait(server,pane,lambda s:'ПЯреобразуй' not in s)
                    self.assertEqual(cursor(),'3 5 1')
                    server.command('send-keys','-t',pane,'Tab')
                    self.wait(server,pane,lambda s:'редактирование' not in s)
                    self.assertNotEqual(cursor().split()[1],'5')
                    server.command('send-keys','-t',pane,'F4','Enter')
                    self.wait(server,pane,lambda s:'редактирование' in s and 'ПРЕСЕТЫ' not in s)
                    self.assertEqual(cursor(),'2 5 1')
                    self.assertIsNone(session.snapshot()['last_run'])
                    session.control('close')
                finally:server.stop()
