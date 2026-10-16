"""One explicitly authorized generator diagnostic; never resumes vehicle C."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
import unittest

from week12.src.active_cluster.execution import (
    DEFAULT_OUT, EXPECTED_SPEC, SPEC, Route, Stop, read, save, sha, stamp, source_paths)
from week12.src.active_cluster.frontend import frontend_script, frontend_evidence
from week12.src.active_cluster.generator import generate
from week12.src.active_cluster.verify import verify_generated
from week12.src.component_physics.runner import OMC, COMPLEX, SERVICES, MSL
from week12.src.toolchain import _run


class Diagnostic:
    def __init__(self):
        self.directory = DEFAULT_OUT
        self.path = self.directory/'ledger.json'
        self.began = time.monotonic()
        self.child_s = 0.0
        self.receipt_path = self.directory/'generator-readiness-authorization.json'
        self.receipt = read(self.receipt_path)
        self.proposal_path = self.directory/'generator-readiness-diagnostic-proposal.json'
        self.proposal = read(self.proposal_path)
        self.base_path = self.directory/'ledger-before-readiness-diagnostic.json'
        self.base = read(self.base_path)
        self.refresh()
        self.validate()
        rows = self.scope['attempts']
        if (len(rows) != 1 or rows[0].get('global_ordinal') != 19 or
                rows[0].get('status') != 'RESERVED_EXTERNAL_ROOT'):
            raise Stop('ONE_RESERVED_DIAGNOSTIC_LAUNCHER_REQUIRED_NO_RETRY')
        rows[0].update(status='DISPATCHING', actual_started_utc=stamp())
        self.persist()
        self.folder = self.directory/'readiness-diagnostic-01'
        self.folder.mkdir(exist_ok=False)

    def refresh(self):
        self.data = read(self.path)
        self.scope = self.data['generator_readiness_diagnostic']

    def persist(self):
        self.data['last_checkpoint_utc'] = stamp()
        save(self.path, self.data)

    def validate(self):
        if (self.receipt.get('status') != 'APPROVED_BY_USER' or
                self.receipt.get('user_message_verbatim') != '已确认继续执行' or
                self.receipt.get('approved_proposal_sha256') != sha(self.proposal_path) or
                self.receipt.get('physical_spec_sha256') != EXPECTED_SPEC or
                sha(SPEC) != EXPECTED_SPEC or
                self.receipt.get('base_ledger_sha256') != sha(self.base_path) or
                self.receipt.get('preparation_sha256') !=
                    sha(self.directory/'generator-readiness-preparation.json') or
                self.receipt.get('budget') != self.proposal['finite_budget'] or
                self.receipt.get('scope') != self.proposal['scope'] or
                self.scope.get('authorization_sha256') != sha(self.receipt_path) or
                self.scope.get('vehicle_resume') is not False or
                not self.data['status'].startswith('STOPPED')):
            raise Stop('DIAGNOSTIC_AUTHORIZATION_OR_SPEC_DRIFT')
        for field in ('started_utc','future_cap_s','implementation_analysis_cap_s',
                      'process_cap_s','scene_cap','parse_cap','other_check_cap','repair_rounds_cap'):
            if self.data[field] != self.base[field]:
                raise Stop('DIAGNOSTIC_CANNOT_EXPAND_OR_RESET_OLD_CAPS')
        for key in ('scene_attempts','parse_attempts','other_checks','repairs','source_bindings'):
            if self.data[key] != self.base[key]:
                raise Stop('DIAGNOSTIC_CANNOT_CHANGE_VEHICLE_HISTORY')
        if self.data['active_intervals'][:len(self.base['active_intervals'])] != self.base['active_intervals']:
            raise Stop('DIAGNOSTIC_CANNOT_RESET_ACTIVE_TIME_HISTORY')
        preparation = read(self.directory/'generator-readiness-preparation.json')
        for row in preparation['sources']:
            if sha(SPEC.parents[3]/row['path']) != row['sha256']:
                raise Stop('PREPARED_SOURCE_DRIFT')

    def limits(self, reserve_child=0):
        self.refresh(); self.validate()
        budget = self.receipt['budget']
        parent = max(0, time.monotonic()-self.began-self.child_s)
        diagnostic_process = parent+self.child_s
        route = Route()
        active = route.elapsed()
        original_process = route.process_used()
        resumed = datetime.fromisoformat(self.receipt['resumed_utc'].replace('Z','+00:00'))
        packet_active = self.scope['preparation_spent_s'] + (datetime.now(timezone.utc)-resumed).total_seconds()
        if (packet_active+reserve_child > budget['whole_package_active_s'] or
                active+reserve_child > self.data['future_cap_s'] or
                original_process+diagnostic_process+reserve_child > self.data['process_cap_s'] or
                max(0,active-original_process-diagnostic_process) >= self.data['implementation_analysis_cap_s'] or
                parent >= budget['Python_launcher_parent_overhead_s_max'] or
                diagnostic_process+reserve_child > budget['all_external_process_aggregate_s_max']):
            raise Stop('FINITE_DIAGNOSTIC_TIME_EXHAUSTED')

    def sources(self, manifest):
        paths = source_paths() + [Path(__file__),
            SPEC.parents[3]/'week12/tests/test_active_cluster_frontend.py',
            COMPLEX, SERVICES, MSL, self.receipt_path, self.proposal_path]
        paths += [Path(row['path']) for row in manifest['sources']]
        return [{'path': str(path), 'sha256': sha(path)}
                for path in sorted(set(paths), key=str)]

    @staticmethod
    def assert_sources(rows):
        if any(sha(row['path']) != row['sha256'] for row in rows):
            raise Stop('DIAGNOSTIC_SOURCE_DRIFT')

    def run(self):
        outcome = {'status':'FAIL','compile':'NOT_RUN','initialization':'NOT_RUN',
                   'simulation':'NOT_RUN','vehicle_task':'NOT_RUN',
                   'vehicle_resume':False,'physical_spec_sha256':EXPECTED_SPEC}
        try:
            self.limits()
            began = time.monotonic()
            modules = ['week12.tests.test_active_cluster_execution',
                       'week12.tests.test_active_cluster_generation',
                       'week12.tests.test_active_cluster_verification',
                       'week12.tests.test_active_cluster_frontend']
            with (self.folder/'unittest.log').open('w', encoding='utf-8') as stream:
                tests = unittest.TextTestRunner(stream=stream,verbosity=2).run(
                    unittest.TestLoader().loadTestsFromNames(modules))
            unit = {'status':'PASS' if tests.wasSuccessful() else 'FAIL',
                    'count':tests.testsRun,'failures':len(tests.failures),'errors':len(tests.errors),
                    'duration_s':time.monotonic()-began,'execution':'same launcher process'}
            save(self.folder/'unit-result.json',unit); outcome['units'] = unit
            if not tests.wasSuccessful() or tests.testsRun != 51:
                raise Stop('DIAGNOSTIC_UNIT_GATE_FAILED_NO_RETRY')
            self.limits()
            manifest = generate(SPEC, self.folder/'generated')
            graph = read(manifest['design_path'])
            generated = verify_generated(graph,manifest,read(SPEC))
            save(self.folder/'generation-review.json',generated)
            outcome['generation'] = generated['status']
            if generated['status'] != 'PASS':
                raise Stop('DIAGNOSTIC_GENERATION_GATE_FAILED')
            parsed = read(self.directory/'parse-round-03/parse.json')
            identical = (sha(manifest['sysml_path']) ==
                         sha(self.directory/'generated-round-03/candidate.sysml'))
            outcome['sysml'] = {'byte_identical':identical,'prior_actual_status':parsed.get('status'),
                                'method':'reuse only exact bytes previously parsed; no new parse'}
            if not identical or parsed.get('status') != 'PASS':
                raise Stop('DIAGNOSTIC_SYSML_BYTES_NOT_REUSABLE')
            source_rows = self.sources(manifest)
            save(self.folder/'source-snapshot.json',source_rows)
            (self.folder/'frontend.mos').write_text(frontend_script(manifest),encoding='utf-8')
            self.limits(reserve_child=60)
            self.assert_sources(source_rows)
            self.refresh()
            if len(self.scope['attempts']) != 1:
                raise Stop('ONE_OMC_DIAGNOSTIC_ATTEMPT_NO_RETRY')
            self.scope['attempts'].append({'ordinal':2,'global_ordinal':20,'kind':'OMC_FRONTEND_ONLY',
                'status':'RUNNING','reserved_wall_s':60,'started_utc':stamp(),'processes':[]})
            self.persist()
            result = _run([str(OMC),'frontend.mos'],self.folder,60)
            self.child_s = float(result['duration_s'])
            for stream in ('stdout','stderr'):
                (self.folder/f'frontend.{stream}.log').write_text(result[stream],encoding='utf-8')
            self.refresh()
            child = self.scope['attempts'][1]
            child.update(status='RETURNED',ended_utc=stamp(),processes=[
                {k:v for k,v in result.items() if k not in ('stdout','stderr')}])
            self.persist()
            evidence = frontend_evidence(manifest,result)
            if self.child_s > 60:
                evidence['status']='FAIL'; evidence['wall_limit_exceeded']=True
            save(self.folder/'frontend-result.json',evidence)
            outcome['frontend'] = evidence
            self.assert_sources(source_rows); self.limits()
            if evidence['status'] != 'PASS_FRONTEND_ONLY':
                raise Stop('DIAGNOSTIC_FRONTEND_FAILED_NO_REPAIR')
            outcome['status']='PASS_FRONTEND_ONLY'
        except Exception as error:
            outcome['error']=str(error)
        finally:
            parent = max(0,time.monotonic()-self.began-self.child_s)
            if parent>30 or parent+self.child_s>90:
                outcome['status']='FAIL';outcome['error']='DIAGNOSTIC_PROCESS_TIME_LIMIT'
            outcome['parent_overhead_s']=parent;outcome['child_s']=self.child_s
            save(self.folder/'result.json',outcome)
            self.refresh()
            row=self.scope['attempts'][0]
            row.update(status=outcome['status'],ended_utc=stamp(),processes=[{
                'duration_s':parent,'dispatcher_overhead':True,'label':'diagnostic Python parent excluding OMC'}])
            self.scope.update(status=outcome['status'],result=str(self.folder/'result.json'),
                              vehicle_resume=False)
            self.persist()
        print(outcome['status'],outcome.get('error',''),flush=True)
        return 0 if outcome['status']=='PASS_FRONTEND_ONLY' else 2


if __name__ == '__main__':
    try:
        raise SystemExit(Diagnostic().run())
    except Exception as error:
        print(type(error).__name__+': '+str(error),flush=True)
        raise SystemExit(2)
