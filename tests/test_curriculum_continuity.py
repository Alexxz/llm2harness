"""Guard the interface knowledge introduced after the revised lesson 00."""
import json
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]


class CurriculumContinuityTests(unittest.TestCase):
    def test_action_hotkeys_have_been_explained_before_the_action(self):
        known={'F1','F2','F3','F4','F5','F6','Esc','Enter'}
        pattern=re.compile(r'\b(?:F\d+|Tab|Shift\+Tab|Home|End|Backspace)\b')
        for ident in ('01','02','03','04'):
            d=next((ROOT/'lessons').glob(ident+'-*'))
            spec=json.loads((d/'workspace.json').read_text())
            for phase in spec['phases']:
                known.update(pattern.findall(phase['explanation']))
                self.assertTrue(set(pattern.findall(phase['action']))<=known,(ident,phase['name']))

    def test_temperature_pairs_change_one_factor_inside_each_pair(self):
        d=ROOT/'lessons/01-black-box'
        spec=json.loads((d/'workspace.json').read_text())
        pairs=[p for p in spec['phases'] if any('parameters' in e for e in p['evidence'])]
        self.assertEqual(len(pairs),2)
        for phase,seed in zip(pairs,(42,43)):
            conditions=[e['parameters'] for e in phase['evidence']]
            self.assertEqual({c['seed'] for c in conditions},{seed})
            self.assertEqual({c['max_tokens'] for c in conditions},{32})
            self.assertEqual({c['temperature'] for c in conditions},{0,.9})

    def test_new_training_lesson_does_not_silently_feed_old_lesson_five(self):
        d=ROOT/'lessons/05-fine-tuning'
        self.assertIn('не создаёт старый work/training-before.json',(d/'INTRO.md').read_text())
        self.assertIn('work/training-before.json',(d/'LABORATORY_PLAN.md').read_text())
        self.assertEqual(json.loads((d/'lesson.json').read_text())['workspace_migration']['status'],'design_reviewed_not_implemented')
