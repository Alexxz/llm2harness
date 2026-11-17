from pathlib import Path
import tempfile
import unittest
from coursekit.registry import validate_delivery_contract

class LessonContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.item={'id':'02','delivery_contract':'interactive-v1','artifacts':['TEACHER_START.md','EXPLANATIONS.md','AFTER_SHOW.md']}
        for name in self.item['artifacts']:(self.root/name).write_text('Prepared teaching text.')
    def test_entry_assets_fit_the_runtime_limits(self):
        self.assertEqual(validate_delivery_contract(self.root,self.item),[])
    def test_missing_or_empty_explanation_is_not_a_ready_lesson(self):
        (self.root/'EXPLANATIONS.md').write_text(' ')
        self.assertTrue(any('пустой' in error for error in validate_delivery_contract(self.root,self.item)))
        (self.root/'TEACHER_START.md').unlink()
        self.assertTrue(any('TEACHER_START.md' in error for error in validate_delivery_contract(self.root,self.item)))
    def test_oversize_card_is_rejected_before_opening_the_lesson(self):
        (self.root/'AFTER_SHOW.md').write_text('x'*8193)
        self.assertTrue(any('runtime' in error for error in validate_delivery_contract(self.root,self.item)))
