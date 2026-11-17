import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coursekit.classroom import _read

class AtomicReadTests(unittest.TestCase):
    def test_opened_inode_replaced_atomically_is_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'session.json';target.write_bytes(b'old')
            real_open=os.open;replaced=False
            def racing_open(path,*args,**kwargs):
                nonlocal replaced
                fd=real_open(path,*args,**kwargs)
                if Path(path)==target and not replaced:
                    replaced=True
                    update=root/'replacement';update.write_bytes(b'new');os.replace(update,target)
                return fd
            with patch('coursekit.classroom.os.open',side_effect=racing_open):
                self.assertEqual(_read(target,root,100),b'new')
    def test_hardlinks_remain_forbidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'session.json';target.write_bytes(b'value');os.link(target,root/'other')
            with self.assertRaises(ValueError):_read(target,root,100)
