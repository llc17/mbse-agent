"""Read an already generated candidate; no generation, OMC or simulation here.

V11_EVENT_CANDIDATE_DIR names the parent ledger's new artifact directory.
The finite event check compares parsed original/candidate event relations,
including held values and rising-edge memory. It is not an OMC runtime proof.
"""
import itertools
import json
import os
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / 'week12/results/s3-c-active-eightwheel-execution-20261009-01/eightwheel-signed-work-execution-20261010'
OWNED = ('segment', 'loadedSince', 'pausedSince', 'exitCrossed', 'exitReferenceX', 'completionTime')


def scenes(text):
    return dict(re.findall(r'  model (\w+)\n(.*?)  end \1;', text, re.S))


def blocks(scene):
    return scene.split('\n  algorithm\n')[1:]


def events(block):
    return re.findall(r'\bwhen\s+(.*?)\s+then\s+(.*?)\bend\s+when\s*;', block, re.S)


def evaluate(expression, current, previous, initial):
    expression = expression.strip()
    if expression.startswith('{'):
        return any(evaluate(x, current, previous, initial) for x in expression[1:-1].split(','))
    expression = expression.replace('initial()', str(initial))
    expression = re.sub(r'pre\((\w+)\)', lambda m: repr(previous[m[1]]), expression)
    expression = re.sub(r'change\((\w+)\)', lambda m: str(current[m[1]] != previous[m[1]]), expression)
    expression = re.sub(r'\btrue\b', 'True', expression)
    expression = re.sub(r'\bfalse\b', 'False', expression)
    expression = expression.replace('<>', '!=')
    match = re.fullmatch(r'if (.*?) then (.*?) else (.*)', expression, re.S)
    if match:
        return evaluate(match[2] if evaluate(match[1], current, previous, initial) else match[3], current, previous, initial)
    return eval(expression, {'__builtins__': {}, 'min': min, 'max': max}, current)


def relation(algorithm_blocks, current, previous, initial, edges):
    """Evaluate one event relation with a fixed shared pre/condition memory.

    Outputs start at pre on every invocation. Monolithic later statements see
    earlier local writes; separate blocks see the proposed fixed-point inputs.
    edges records the unchanged rising-edge status of each when branch.
    """
    outputs = {}
    event_index = 0
    for block in algorithm_blocks:
        assigned = set(re.findall(r'\b(\w+)\s*:=', block))
        local = dict(current)
        local.update({key: previous[key] for key in assigned})
        for condition, body in events(block):
            branches = [(condition, body)]
            if 'elsewhen ' in body:
                first, tail = re.split(r'\belsewhen\s+', body, maxsplit=1)
                second_condition, second = re.split(r'\s+then\s+', tail, maxsplit=1)
                branches = [(condition, first), (second_condition, second)]
            for branch_index, (guard, statements) in enumerate(branches):
                active = evaluate(guard, local, previous, initial)
                # initial() dominates elsewhen; other guards use shared
                # pre(condition), so persistent true guards do not refire.
                if active and (initial or edges[event_index][branch_index]):
                    for key, rhs in re.findall(r'\b(\w+)\s*:=\s*([^;]+);', statements):
                        local[key] = evaluate(rhs, local, previous, initial)
                    break
            event_index += 1
        outputs.update({key: local[key] for key in assigned})
    return outputs


class EventAlgorithmRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidate = Path(os.environ['V11_EVENT_CANDIDATE_DIR'])
        cls.new = (candidate / 'V11GeneratedActive.mo').read_text(encoding='utf-8')
        cls.old = (RUN / 'module-compiler-candidate-02/V11GeneratedActive.mo').read_text(encoding='utf-8')
        cls.new_scenes, cls.old_scenes = scenes(cls.new), scenes(cls.old)
        cls.new_contract = json.loads((candidate / 'output-contract.json').read_bytes())
        cls.old_contract = json.loads((RUN / 'module-compiler-candidate-02/output-contract.json').read_bytes())

    def test_only_added_plan_algorithm_boundaries_and_exact_contract(self):
        restored = self.new
        for name, scene in self.new_scenes.items():
            old = self.old_scenes[name]
            if name in ('Probe', 'Flat'):
                self.assertEqual(scene, old)
            else:
                first, *remaining = scene.split('\n  algorithm\n')
                merged = first + '\n  algorithm\n' + '\n'.join(remaining)
                self.assertEqual(merged, old)
                restored = restored.replace(scene, merged, 1)
        self.assertEqual(restored, self.old)
        self.assertEqual(self.new_contract, self.old_contract)

    def test_unique_ownership_and_exact_event_statement_order(self):
        expected = [{'segment'}, {'loadedSince'}, {'pausedSince'}, {'exitCrossed', 'exitReferenceX'}, {'completionTime'}]
        for name, scene in self.new_scenes.items():
            if name in ('Probe', 'Flat'):
                continue
            algorithm_blocks = blocks(scene)
            self.assertEqual([set(re.findall(r'\b(\w+)\s*:=', x)) for x in algorithm_blocks], expected)
            self.assertEqual([event for block in algorithm_blocks for event in events(block)], events(blocks(self.old_scenes[name])[0]))

    def test_simultaneous_event_relations_and_holding_at_stage_fixed_point(self):
        # Inputs stand for unchanged algebraic/continuous equations. Compare
        # both event relations for every Boolean combination, both directions,
        # first/middle/final stage, rising and already-true condition memory.
        for name, scene in self.new_scenes.items():
            if name in ('Probe', 'Flat'):
                continue
            old_blocks = blocks(self.old_scenes[name])
            new_blocks = blocks(scene)
            initial_stage = int(re.search(r'when initial\(\) then\s+segment:=(\d+);', scene)[1])
            ascending = 'min(' in events(new_blocks[0])[0][1]
            stage_limit = int(re.search(r'min\((\d+),pre\(segment\)\+1\)', scene)[1]) if ascending else initial_stage
            for pre_stage in sorted({1, max(1, stage_limit//2), stage_limit}):
                for initial, guard, failed, load, pause, done, cross, rising in itertools.product((False, True), repeat=8):
                    previous = dict(segment=pre_stage, loadedSince=2., pausedSince=3., exitCrossed=False, exitReferenceX=4., completionTime=-1., loadOK=not load, paused=not pause)
                    current = dict(previous, initial=initial, guardReady=guard, failed=failed, loadOK=load, paused=pause, taskDone=done, referenceClock=10., begin=[None]+[10.]*stage_limit, edge=[None]+[10.]*stage_limit, time=11., wholeExitMinX=(1e9 if cross else -1e9), chassis=type('Chassis', (), {'x': 12.})())
                    edges = [(True, rising), (rising,), (rising,), (True, rising), (True, rising)]
                    mono = relation(old_blocks, current, previous, initial, edges)
                    # Eliminate the stage equation: original local stage value
                    # equals the shared stage input at any event fixed point.
                    current['segment'] = mono['segment']
                    split = relation(new_blocks, current, previous, initial, edges)
                    self.assertEqual(mono, split, (name, pre_stage, initial, guard, failed, load, pause, done, cross, rising))
                    if initial:
                        self.assertEqual(split['segment'], initial_stage)
                        self.assertFalse(split['exitCrossed'])
                        self.assertEqual(split['exitReferenceX'], 0)
                        self.assertEqual(split['completionTime'], -1)
                    elif not rising:
                        self.assertEqual(split, {key: previous[key] for key in OWNED})


if __name__ == '__main__':
    unittest.main()
