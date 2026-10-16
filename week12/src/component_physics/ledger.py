"""Exclusive, durable attempt reservations. Preparation never authorizes execution."""
from __future__ import annotations
import json
from pathlib import Path
from .contract import ROOT, canonical, PROTOCOL_SHA


class Ledger:
    def __init__(self, directory, *, test_only=False):
        self.directory = Path(directory).resolve()
        self.test_only = bool(test_only)
        if not self.directory.is_relative_to(ROOT / 'results'):
            raise ValueError('INVALID: ledger outside V11 results')

    def prepare(self, *, historical_attempts=1):
        if type(historical_attempts) is not int or historical_attempts < 0:
            raise ValueError('INVALID: historical count')
        self.directory.mkdir(parents=True, exist_ok=False)
        self._save('header.json', {'protocol_file_sha256': PROTOCOL_SHA,
                                  'historical_attempts': historical_attempts,
                                  'synthetic_test_only': self.test_only,
                                  'execution_approval': None, 'status': 'PREPARED_NOT_AUTHORIZED'})

    def reserve(self, scenario, *, approval):
        if not isinstance(approval, dict) or approval.get('purpose') != 'b_component_execution':
            raise ValueError('PENDING: explicit concrete batch approval required')
        if approval.get('receipt_kind') != ('synthetic_test' if self.test_only else 'user_conversation'):
            raise ValueError('INVALID: synthetic receipts cannot approve real execution')
        if approval.get('protocol_file_sha256') != PROTOCOL_SHA or not approval.get('user_message'):
            raise ValueError('STALE: approval binding')
        order = approval.get('run_order')
        limit = approval.get('attempt_limit')
        if not isinstance(order, list) or type(limit) is not int or limit < 1 or limit > len(order):
            raise ValueError('INVALID: bounded order/attempt limit')
        if scenario not in order:
            raise ValueError('UNSUPPORTED: unapproved scenario')
        # Exclusive lock prevents two processes from reserving the same next slot.
        lock = self.directory / 'reservation.lock'
        with lock.open('x', encoding='utf-8') as handle:
            handle.write(scenario)
        try:
            header = json.loads((self.directory / 'header.json').read_text(encoding='utf-8'))
            previous = sorted(self.directory.glob('attempt-*.json'))
            if len(previous) >= limit:
                raise ValueError('BUDGET_EXHAUSTED')
            if previous and json.loads(previous[-1].read_text(encoding='utf-8'))['status'] != 'PASS':
                raise ValueError('STOPPED: previous attempt incomplete or failed')
            if scenario != order[len(previous)]:
                raise ValueError('INVALID: execution order')
            if header['execution_approval'] not in (None, approval):
                raise ValueError('STALE: cannot replace batch approval')
            if header['execution_approval'] is None:
                header['execution_approval'] = approval
                header['status'] = 'ACTIVE'
                self._save('header.json', header)
            number = len(previous)+1
            record = {'scenario': scenario, 'batch_slot': number,
                      'cumulative_slot': header['historical_attempts']+number,
                      'status': 'RESERVED', 'simulation_started': False}
            path = self.directory / f'attempt-{number:02d}.json'
            with path.open('xb') as handle:
                handle.write(canonical(record))
                handle.flush()
                __import__('os').fsync(handle.fileno())
            return record
        finally:
            lock.unlink()  # fixed single lock owned by this reservation, no recursive removal

    def finish(self, number, status, *, simulation_started=False, evidence=None):
        if type(number) is not int or number < 1 or status not in ('PASS', 'FAIL', 'INTERRUPTED', 'BLOCKED'):
            raise ValueError('INVALID: completion')
        name = f'attempt-{number:02d}.json'
        record = json.loads((self.directory / name).read_text(encoding='utf-8'))
        if record['status'] != 'RESERVED':
            raise ValueError('INVALID: completed attempt is immutable')
        record.update(status=status, simulation_started=simulation_started, evidence=evidence)
        self._save(name, record)
        return record

    def _save(self, name, value):
        # temp and destination stay in the same directory; replace is atomic.
        temporary = self.directory / (name+'.pending')
        with temporary.open('xb') as handle:
            handle.write(canonical(value))
            handle.flush()
            __import__('os').fsync(handle.fileno())
        temporary.replace(self.directory / name)
