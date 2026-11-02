"""Atomic same-ledger reservations adapted from V10 runtime and V12 paths.

Amounts are CNY. Unknown usage keeps its complete reservation. A reservation is
not an HTTP/process launch: launch timestamps and state are recorded separately.
"""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import uuid
from .case_paths import BUDGET_DB, RUN, TASK_CONTEXT, authorization, authorization_sha, require_time, utc_now, sha_value, save_json
from .case_requests import load_request, authority_for, require_planning, require_execution


@contextmanager
def connection(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


class CaseBudget:
    def __init__(self, ledger_path=None, *, task_context=None):
        self.task_context = load_request((task_context or TASK_CONTEXT)['request_id']) if (task_context or TASK_CONTEXT) else None
        self.authority = authority_for(self.task_context) if self.task_context else authorization()
        canonical = Path(self.authority['ledger_path']) if self.task_context else BUDGET_DB
        self.path = Path(ledger_path or canonical)
        if self.task_context and self.path.resolve() != canonical.resolve():
            raise PermissionError('new requests must retain the budget authorization unique ledger')
        self._authorization_sha = (lambda: __import__('hashlib').sha256(Path(self.task_context['runtime_authority']['path']).read_bytes()).hexdigest()) if self.task_context else authorization_sha
        binding = None
        if self.path.resolve() == canonical.resolve():
            binding = self.path.parent/'budget-binding.json' if self.task_context else RUN/'budget-binding.json'
            if binding.exists() and not self.path.exists():
                raise ValueError('original V12 budget ledger is missing; refuse to recreate/reset')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with connection(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS requests(request_id TEXT PRIMARY KEY,content_hash TEXT NOT NULL,category TEXT NOT NULL,status TEXT NOT NULL,reserve_tokens INTEGER NOT NULL,reserve_output INTEGER NOT NULL,reserve_cost REAL NOT NULL,actual_tokens INTEGER,actual_output INTEGER,actual_cost REAL,response TEXT,reason TEXT,reserved_at TEXT NOT NULL,launched_at TEXT,finished_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS process_attempts(attempt_id TEXT PRIMARY KEY,kind TEXT NOT NULL,detail_json TEXT NOT NULL,state TEXT NOT NULL,reserved_at TEXT NOT NULL,launched_at TEXT,pid INTEGER,finished_at TEXT,result_json TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS counters(kind TEXT NOT NULL,event_id TEXT NOT NULL,detail_json TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(kind,event_id))')
            db.execute('CREATE TABLE IF NOT EXISTS http_recoveries(request_id TEXT PRIMARY KEY,original_row_json TEXT NOT NULL,proof_json TEXT NOT NULL,recovered_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS connect_retries(old_request_id TEXT PRIMARY KEY,new_request_id TEXT UNIQUE NOT NULL,content_hash TEXT NOT NULL,proof_json TEXT NOT NULL,authorized_at TEXT NOT NULL,launched_at TEXT)')
            if self.authority.get('operation_policy'):db.execute('CREATE TABLE IF NOT EXISTS readonly_attempts(request_id TEXT PRIMARY KEY,context_id TEXT NOT NULL,step_id TEXT NOT NULL)')
            current = self._authorization_sha()
            old = db.execute("SELECT value FROM meta WHERE key='authorization_sha256'").fetchone()
            if old and old[0] != current:
                raise ValueError('V12 budget belongs to a different authorization; no reset')
            db.execute("INSERT OR IGNORE INTO meta VALUES('authorization_sha256',?)", (current,))
        if binding is not None:
            save_json(binding, dict(schema='v12-single-budget-binding-1', ledger=str(self.path.resolve()),
                                   authorization_sha256=self._authorization_sha()), immutable=True)

    def _require_time(self):
        if self.task_context:
            authority=authority_for(load_request(self.task_context['request_id']))
            from datetime import timezone
            current=datetime.now(timezone.utc)
            start=datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))
            end=datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00'))
            if not start <= current < end:
                raise RuntimeError('request shared authorization wall-clock interval is not active')
        else:
            require_time()

    def request(self, request_id):
        with connection(self.path) as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None

    def _blocked(self, db, request_id=None):
        frozen = db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()
        if frozen:
            if not self._http_rejection_diagnostic_allowed(db, request_id) and not self._http_schema_correction_allowed(db,request_id) and not self._plan_read_timeout_allowed(db,request_id):
                raise RuntimeError('V12 runtime frozen: ' + frozen[0] + '; readback only, no blind resend')
        pending = db.execute("SELECT value FROM meta WHERE key='connect_retry_required'").fetchone()
        if pending and pending[0] != request_id:
            raise RuntimeError('only the explicitly bound connection-phase replacement may dispatch')
        if self.authority.get('operation_policy'):
            for row in db.execute("SELECT r.request_id,a.context_id FROM requests r LEFT JOIN readonly_attempts a USING(request_id) WHERE r.status='unknown'"):
                if not row['context_id'] or not self._local_exit_proven(row['request_id'],row['context_id']):raise RuntimeError('readonly isolation lacks confirmed local process exit; whole package stopped')

    def _plan_read_timeout_allowed(self,db,request_id=None):
        from .case_plan_recovery import registered
        return registered(db,self.task_context,self.authority,request_id) is not None

    def effective_http_transport(self,request_id):
        names=('connect_timeout_s','read_timeout_s','no_progress_timeout_s','whole_request_deadline_s')
        controls={name:self.authority['model_controls'][name] for name in names}
        with connection(self.path) as db:
            from .case_plan_recovery import registered
            proof=registered(db,self.task_context,self.authority,request_id)
        if proof and request_id==proof['replacement_http_request_id']:
            return dict(proof['effective_transport']),dict(approval_path=proof['approval_path'],approval_sha256=proof['approval_sha256'],proof_sha256=sha_value(proof))
        return controls,None

    def authorize_plan_read_timeout(self,proof):
        """Append one explicit exception. Never delete the original freeze/row."""
        from .case_plan_recovery import approved
        from .case_taskflow import TaskFlow
        self._require_time()
        if not self.task_context:raise PermissionError('bound initial planning context required')
        _,verified=approved(TaskFlow.load(self.task_context['request_id']),proof['approval_path'])
        if verified!=proof or proof['ledger_path']!=str(self.path.resolve()):raise PermissionError('replacement approval proof differs')
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS plan_read_timeout_replacements(request_context_id TEXT PRIMARY KEY,replacement_request_id TEXT UNIQUE NOT NULL,proof_json TEXT NOT NULL,recorded_at TEXT NOT NULL)')
            if db.execute('SELECT 1 FROM plan_read_timeout_replacements').fetchone():raise PermissionError('one explicit read-timeout replacement only')
            db.execute('INSERT INTO plan_read_timeout_replacements VALUES(?,?,?,?)',(self.task_context['request_id'],proof['replacement_http_request_id'],json.dumps(proof,ensure_ascii=False,sort_keys=True),utc_now()))
            if not self._plan_read_timeout_allowed(db,proof['replacement_http_request_id']):raise PermissionError('replacement evidence changed before registration')
        return proof

    def _http_rejection_diagnostic_allowed(self, db, request_id):
        """One append-only exception, never removal of UNKNOWN or freeze."""
        if not request_id or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='http_rejection_proofs'").fetchone():return False
        row=db.execute('SELECT * FROM http_rejection_proofs WHERE diagnostic_request_id=?',(request_id,)).fetchone()
        if not row:return False
        proof=json.loads(row['proof_json'])
        old=db.execute('SELECT * FROM requests WHERE request_id=?',(row['original_request_id'],)).fetchone()
        new=db.execute('SELECT * FROM requests WHERE request_id=?',(request_id,)).fetchone()
        meta=dict(db.execute('SELECT key,value FROM meta'))
        if not old or sha_value(dict(old))!=proof['original_request_row_sha256'] or old['status']!='unknown' or meta.get('frozen')!=proof['original_freeze'] or row['content_hash']!=old['content_hash']:return False
        if proof.get('authority_sha256')!=self._authorization_sha() or proof.get('ledger_path')!=str(self.path.resolve()):return False
        if not self.task_context or proof.get('request_context_id')!=self.task_context['request_id'] or proof.get('request_digest')!=self.task_context['request_digest']:return False
        if any('closed' in key.lower() or 'retired' in key.lower() or 'CLOSED' in str(value).upper() or 'RETIRED' in str(value).upper() for key,value in meta.items()):return False
        if new and (new['status']!='reserved' or new['launched_at'] is not None or new['content_hash']!=row['content_hash']):return False
        if db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') AND request_id NOT IN (?,?)",(old['request_id'],request_id)).fetchone():return False
        try:
            from .case_paths import sha_file
            if any(sha_file(Path(name))!=expected for name,expected in proof['evidence_hashes'].items()):return False
        except (OSError,ValueError):return False
        return True

    def _http_schema_correction_allowed(self,db,request_id):
        if not self.task_context or not db.execute("SELECT 1 FROM sqlite_master WHERE name='http_schema_correction_proofs'").fetchone():return False
        row=db.execute('SELECT * FROM http_schema_correction_proofs').fetchone()
        if not row:return False
        proof=json.loads(row['proof_json']);meta=dict(db.execute('SELECT key,value FROM meta'))
        if proof.get('authority_sha256')!=self._authorization_sha() or proof.get('ledger_path')!=str(self.path.resolve()) or proof.get('approved_context_bindings')!=self.authority.get('context_bindings') or self.authority.get('context_bindings',{}).get(self.task_context['request_id'])!=self.task_context['request_digest']:return False
        if meta.get('frozen')!=proof['original_freeze'] or meta.get('connect_retry_required') or any('closed' in k.lower() or 'retired' in k.lower() or 'CLOSED' in str(v).upper() or 'RETIRED' in str(v).upper() for k,v in meta.items()):return False
        for original,expected in proof['preserved_unknown_rows'].items():
            old=db.execute('SELECT * FROM requests WHERE request_id=?',(original,)).fetchone()
            if not old or sha_value(dict(old))!=expected or old['status']!='unknown':return False
        if db.execute('SELECT 1 FROM requests WHERE (actual_tokens>reserve_tokens OR actual_output>reserve_output OR actual_cost>reserve_cost)').fetchone():return False
        allowed=set(proof['preserved_unknown_rows'])
        pending=[r['request_id'] for r in db.execute("SELECT request_id FROM requests WHERE status IN ('unknown','reserved','dispatched')")]
        if any(rid not in allowed and rid!=request_id for rid in pending):return False
        try:
            from .case_paths import sha_file
            if any(sha_file(Path(name))!=expected for name,expected in proof['evidence_hashes'].items()):return False
        except (OSError,ValueError):return False
        correction=db.execute('SELECT * FROM requests WHERE request_id=?',(row['correction_request_id'],)).fetchone()
        if request_id==row['correction_request_id']:
            return self.task_context['request_id']==proof['request_context_id'] and (not correction or correction['status']=='reserved' and correction['launched_at'] is None and correction['content_hash']==row['content_hash'])
        if not correction or correction['status']!='succeeded':return False
        try:
            from .case_paths import read_json
            current=load_request(proof['request_context_id']);validation=read_json(Path(proof['origin_request_root'])/'plan-validation.json')
            packet=current.get('plan',{})
            if current['baseline_ref']!=proof['baseline_ref'] or sha_value(packet)!=validation.get('packet_digest') or validation.get('executable') is not True or validation.get('after')!=proof['expected_parameters'] or packet.get('scope')!=proof['expected_scope'] or packet.get('support')!='SUPPORTED' or packet.get('intent')!='CHANGE' or packet.get('missing') or packet.get('unsupported'):return False
        except (OSError,ValueError,KeyError,TypeError):return False
        return request_id is None or request_id.endswith('_'+self.task_context['request_id'])

    def authorize_http_schema_correction(self,correction_request_id,content_hash,proof):
        """One known empty-object correction inside repair1; preserve both UNKNOWNs."""
        self._require_time()
        if not self.task_context or proof.get('schema')!='v12-http-empty-object-correction-proof-1' or proof.get('authority_sha256')!=self._authorization_sha() or proof.get('request_context_id')!=self.task_context['request_id'] or proof.get('request_digest')!=self.task_context['request_digest'] or proof.get('ledger_path')!=str(self.path.resolve()) or proof.get('correction_request_id')!=correction_request_id or proof.get('new_payload_sha256')!=content_hash:raise PermissionError('known schema correction binding differs')
        if correction_request_id!='case_plan_'+self.task_context['request_id']+'_empty_object_correction_1':raise PermissionError('one fixed corrected plan RID required')
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM counters WHERE kind='software_repair' AND event_id='http400-protocol-repair-1'").fetchone() is None or db.execute("SELECT count(*) FROM counters WHERE kind='software_repair'").fetchone()[0]!=1:raise PermissionError('correction must stay in the already counted repair1')
            oldset=set(proof.get('preserved_unknown_rows',{}));pending={r[0] for r in db.execute("SELECT request_id FROM requests WHERE status IN ('unknown','reserved','dispatched')")}
            if len(oldset)!=2 or pending!=oldset:raise PermissionError('only the two proven HTTP400 UNKNOWNs may be retained')
            if db.execute('SELECT 1 FROM requests WHERE request_id=?',(correction_request_id,)).fetchone():raise PermissionError('corrected plan already attempted')
            count,tokens,output,cost=db.execute("SELECT count(*),coalesce(sum(coalesce(actual_tokens,reserve_tokens)),0),coalesce(sum(coalesce(actual_output,reserve_output)),0),coalesce(sum(coalesce(actual_cost,reserve_cost)),0) FROM requests WHERE status<>'not_dispatched'").fetchone()
            c=self.authority['model_controls'];caps=self.authority['caps']
            if count+3>caps['http_requests'] or tokens+c['context_window_tokens']>caps['total_tokens'] or output+c['max_tokens']>caps['total_output_tokens'] or cost+c['maximum_request_reserve_cny']>caps['product_cny']:raise PermissionError('correction and final slots exceed existing budget')
            db.execute('CREATE TABLE IF NOT EXISTS http_schema_correction_proofs(correction_request_id TEXT PRIMARY KEY,content_hash TEXT NOT NULL,proof_json TEXT NOT NULL,recorded_at TEXT NOT NULL)')
            if db.execute('SELECT 1 FROM http_schema_correction_proofs').fetchone():raise PermissionError('one schema correction only')
            db.execute('INSERT INTO http_schema_correction_proofs VALUES(?,?,?,?)',(correction_request_id,content_hash,json.dumps(proof,ensure_ascii=False,sort_keys=True),utc_now()))
            if not self._http_schema_correction_allowed(db,correction_request_id):raise PermissionError('schema correction evidence no longer verifies')
        return proof

    def authorize_http_rejection_diagnostic(self, old_request_id, diagnostic_request_id, content_hash, proof):
        """Trusted local action: atomically charge repair 1 and bind one RID.

        This does not launch HTTP, settle the old request, delete its freeze or
        authorize ordinary planning/generation. Original reservations persist.
        """
        self._require_time()
        if not self.task_context or self.authority.get('test_only'):raise PermissionError('HTTP400 diagnostic requires current live bound authority')
        if diagnostic_request_id!=old_request_id+'_http400_diagnostic_1':raise ValueError('only the fixed single diagnostic RID is allowed')
        if old_request_id!='case_plan_'+self.task_context['request_id']:raise PermissionError('diagnostic cannot borrow a sibling request')
        if proof.get('schema')!='v12-http400-diagnostic-proof-1' or proof.get('authority_sha256')!=self._authorization_sha() or proof.get('ledger_path')!=str(self.path.resolve()) or proof.get('request_context_id')!=self.task_context['request_id'] or proof.get('diagnostic_request_id')!=diagnostic_request_id or proof.get('payload_sha256')!=content_hash:
            raise ValueError('HTTP400 proof authority/request/payload binding differs')
        if proof.get('request_digest')!=self.task_context['request_digest'] or proof.get('http_status')!=400 or proof.get('local_process_terminated') is not True or proof.get('no_local_tool_side_effects') is not True or proof.get('maximum_diagnostic_requests')!=1 or proof.get('recursive_diagnostics_forbidden') is not True:raise PermissionError('HTTP400 diagnostic evidence contract differs')
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM requests WHERE request_id=?',(old_request_id,)).fetchone()
            meta=dict(db.execute('SELECT key,value FROM meta'))
            if not old or old['status']!='unknown' or old['content_hash']!=content_hash or sha_value(dict(old))!=proof.get('original_request_row_sha256') or any(old[key] is not None for key in ('actual_tokens','actual_output','actual_cost')):raise PermissionError('original UNKNOWN/full reservation must remain exact')
            if meta.get('frozen')!='unknown product HTTP; usage reservation retained' or proof.get('original_freeze')!=meta['frozen']:raise PermissionError('not the known HTTP400 freeze')
            if meta.get('connect_retry_required'):raise PermissionError('connection retry and HTTP400 diagnostic cannot overlap')
            if any('closed' in key.lower() or 'retired' in key.lower() or 'CLOSED' in str(value).upper() or 'RETIRED' in str(value).upper() for key,value in meta.items()):raise PermissionError('closed/retired budget cannot recover')
            if db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') AND request_id<>?",(old_request_id,)).fetchone():raise PermissionError('another UNKNOWN/inflight request blocks diagnostic')
            if db.execute("SELECT 1 FROM process_attempts WHERE state NOT IN ('FINISHED','NOT_LAUNCHED')").fetchone():raise PermissionError('inflight tool/worker process blocks diagnostic')
            if db.execute('SELECT 1 FROM requests WHERE request_id=?',(diagnostic_request_id,)).fetchone():raise PermissionError('diagnostic already attempted')
            allocation=self.authority.get('repair_allocation',{})
            used=db.execute("SELECT count(*) FROM counters WHERE kind='software_repair'").fetchone()[0]
            if used!=0 or allocation.get('maximum_before_first_valid_generated_source')!=1 or allocation.get('reserved_for_post_generation_or_runtime',0)<2 or self.authority['caps']['software_repair_rounds']!=3:raise PermissionError('the one pre-source repair is unavailable')
            if db.execute("SELECT 1 FROM meta WHERE key='first_valid_generated_source'").fetchone():raise PermissionError('this diagnostic is only the first pre-source repair')
            count,tokens,output,cost=db.execute("SELECT count(*),coalesce(sum(coalesce(actual_tokens,reserve_tokens)),0),coalesce(sum(coalesce(actual_output,reserve_output)),0),coalesce(sum(coalesce(actual_cost,reserve_cost)),0) FROM requests WHERE status<>'not_dispatched'").fetchone()
            controls=self.authority['model_controls'];caps=self.authority['caps']
            if count+3>caps['http_requests'] or tokens+controls['context_window_tokens']>caps['total_tokens'] or output+controls['max_tokens']>caps['total_output_tokens'] or cost+controls['maximum_request_reserve_cny']>caps['product_cny']:raise PermissionError('diagnostic full reservation and final two slots do not fit')
            db.execute('CREATE TABLE IF NOT EXISTS http_rejection_proofs(original_request_id TEXT PRIMARY KEY,diagnostic_request_id TEXT UNIQUE NOT NULL,content_hash TEXT NOT NULL,proof_json TEXT NOT NULL,recorded_at TEXT NOT NULL)')
            if db.execute('SELECT 1 FROM http_rejection_proofs').fetchone():raise PermissionError('one HTTP400 diagnostic proof only; no recursive recovery')
            db.execute('INSERT INTO http_rejection_proofs VALUES(?,?,?,?,?)',(old_request_id,diagnostic_request_id,content_hash,json.dumps(proof,ensure_ascii=False,sort_keys=True),utc_now()))
            if not self._http_rejection_diagnostic_allowed(db,diagnostic_request_id):raise PermissionError('HTTP400 proof evidence no longer verifies')
            detail=dict(stage='http400_error_body_protocol_repair',original_request_id=old_request_id,diagnostic_request_id=diagnostic_request_id,proof_sha256=sha_value(proof),original_unknown_and_freeze_preserved=True)
            db.execute('INSERT INTO counters VALUES(?,?,?,?)',('software_repair','http400-protocol-repair-1',json.dumps(detail,ensure_ascii=False,sort_keys=True),utc_now()))
        return proof

    def _local_exit_proven(self,request_id,context_id):
        ctx=load_request(context_id);path=Path(ctx['root'])/'http'/sha_value(request_id)/'process-exit.json'
        if not path.exists():return False
        proof=json.loads(path.read_text(encoding='utf8'))
        if proof.get('request_id')!=request_id:return False
        if proof.get('local_process_terminated') is True and type(proof.get('returncode')) is int:return True
        return self.authority.get('test_only') is True and proof.get('fixture_transport') is True and proof.get('fixture_call_returned') is True and proof.get('process_launched') is False

    def reserve_http(self, request_id, content_hash, category, config):
        self._require_time()
        if self.task_context: require_planning(self.task_context['request_id'])
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            self._blocked(db, request_id)
            if db.execute('SELECT 1 FROM requests WHERE request_id=?', (request_id,)).fetchone():
                raise RuntimeError('request already recorded; cached success/readback only')
            if db.execute("SELECT 1 FROM requests WHERE status IN ('reserved','dispatched')").fetchone():
                raise RuntimeError('another HTTP is reserved or in flight; no concurrent dispatch')
            retry = db.execute('SELECT content_hash FROM connect_retries WHERE new_request_id=?', (request_id,)).fetchone()
            if retry and retry[0] != content_hash:
                raise ValueError('connection-phase replacement payload differs')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='http_rejection_proofs'").fetchone():
                diagnostic=db.execute('SELECT content_hash FROM http_rejection_proofs WHERE diagnostic_request_id=?',(request_id,)).fetchone()
                if diagnostic and diagnostic[0]!=content_hash:raise ValueError('HTTP400 diagnostic must retain the exact original payload')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='http_schema_correction_proofs'").fetchone():
                correction=db.execute('SELECT content_hash FROM http_schema_correction_proofs WHERE correction_request_id=?',(request_id,)).fetchone()
                if correction and correction[0]!=content_hash:raise ValueError('schema correction payload differs from reviewed correction')
            count, tokens, output, cost = db.execute("SELECT count(*),coalesce(sum(coalesce(actual_tokens,reserve_tokens)),0),coalesce(sum(coalesce(actual_output,reserve_output)),0),coalesce(sum(coalesce(actual_cost,reserve_cost)),0) FROM requests WHERE status<>'not_dispatched'").fetchone()
            caps = self.authority['caps']
            # Keep a slot for each still-unfulfilled required user-facing exit.
            policy=self.authority.get('operation_policy')
            if policy:
                step=self.task_context['step_id'];prior=db.execute('SELECT r.* FROM requests r JOIN readonly_attempts a USING(request_id) WHERE a.step_id=? ORDER BY r.reserved_at DESC LIMIT 1',(step,)).fetchone()
                if prior:
                    if prior['status'] in ('unknown','reserved','dispatched'):raise RuntimeError('this readonly step is UNKNOWN/in flight; no new-ID retry')
                    correction=db.execute("SELECT 1 FROM counters WHERE kind='software_repair' AND event_id=?",(request_id,)).fetchone()
                    if not correction or any(prior[key] is None for key in ('actual_tokens','actual_output','actual_cost')):raise RuntimeError('step already attempted; only explicitly recorded known-output correction may dispatch')
                    if 'length' in (prior['reason'] or '').lower():raise RuntimeError('truncated output is not a complete-output correction')
                    if prior['status']=='succeeded':
                        known=json.loads(prior['response'] or 'null');failures=[json.loads(path.read_text(encoding='utf8')) for path in (Path(self.task_context['root'])/'failures').glob('*.json')]
                        if not any(item.get('repairable') is True and item.get('response')==known for item in failures):raise RuntimeError('successful HTTP needs its actual known local validation failure before correction')
                attempted={row[0] for row in db.execute('SELECT DISTINCT step_id FROM readonly_attempts')}
                missing_after=set(policy['steps'])-attempted-{step}
            else:
                succeeded = {row[0] for row in db.execute("SELECT category FROM requests WHERE status='succeeded'")}
                missing_after = {'explanation', 'reopen'} - succeeded - {category}
            if count + 1 + len(missing_after) > caps['http_requests']:
                raise RuntimeError('V12 protects required explanation/reopen HTTP slots')
            reserve_tokens = config['context_window_tokens']
            reserve_output = config['max_tokens']
            reserve_cost = config['maximum_request_reserve_cny']
            if count >= caps['http_requests'] or tokens + reserve_tokens > caps['total_tokens'] or output + reserve_output > caps['total_output_tokens'] or cost + reserve_cost > caps['product_cny']:
                raise RuntimeError('V12 product HTTP/tokens/CNY quota reached')
            db.execute('INSERT INTO requests(request_id,content_hash,category,status,reserve_tokens,reserve_output,reserve_cost,reserved_at) VALUES(?,?,?,?,?,?,?,?)', (request_id, content_hash, category, 'reserved', reserve_tokens, reserve_output, reserve_cost, utc_now()))
            if policy:db.execute('INSERT INTO readonly_attempts VALUES(?,?,?)',(request_id,self.task_context['request_id'],self.task_context['step_id']))

    def launch_http(self, request_id, *, fixture=False):
        self._require_time()
        if self.task_context: require_planning(self.task_context['request_id'])
        if self.authority.get('test_only') is True and not fixture:
            raise PermissionError('test-only authority cannot launch a real product transport')
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            self._blocked(db, request_id)
            changed = db.execute("UPDATE requests SET status='dispatched',launched_at=? WHERE request_id=? AND status='reserved' AND launched_at IS NULL", (utc_now(), request_id)).rowcount
            if changed != 1:
                raise RuntimeError('HTTP not uniquely reserved or already dispatched; no resend')
            db.execute('UPDATE connect_retries SET launched_at=? WHERE new_request_id=? AND launched_at IS NULL', (utc_now(), request_id))

    def finish_http(self, request_id, *, response=None, tokens=None, output=None, cost=None, error=None, uncertain=False):
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if old is None:
                raise RuntimeError('HTTP receipt lacks original reservation')
            if old['status'] not in ('reserved', 'dispatched'):
                raise RuntimeError('HTTP has a final receipt; cannot replace it')
            known = all(value is not None for value in (tokens, output, cost))
            if old['launched_at'] is None:
                status = 'not_dispatched'
            elif uncertain or not known:
                status = 'unknown'
            else:
                status = 'rejected' if error else 'succeeded'
            exceeded=known and (tokens > old['reserve_tokens'] or output > old['reserve_output'] or cost > old['reserve_cost'])
            if status=='unknown' and self.authority.get('operation_policy') and any(value is not None for value in (tokens,output,cost)):
                save_json(Path(self.task_context['root'])/'http'/sha_value(request_id)/'partial-usage-observation.json',dict(request_id=request_id,observed_tokens=tokens,observed_output=output,observed_cost=cost,status='PARTIAL_OBSERVATION_NOT_SETTLEMENT',full_reservation_retained=True),immutable=True)
                tokens=output=cost=None
            db.execute('UPDATE requests SET status=?,actual_tokens=?,actual_output=?,actual_cost=?,response=?,reason=?,finished_at=? WHERE request_id=?', (status, tokens, output, cost, json.dumps(response, ensure_ascii=False) if response is not None else None, error, utc_now(), request_id))
            if status == 'unknown':
                isolated=self.authority.get('operation_policy') and self._local_exit_proven(request_id,self.task_context['request_id'])
                if isolated:db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('isolated_unknown_'+request_id,'readonly step UNKNOWN; full reservation retained; no step retry'))
                else:db.execute("INSERT OR IGNORE INTO meta VALUES('frozen','unknown product HTTP; usage reservation retained')")
            if exceeded:
                db.execute("INSERT OR IGNORE INTO meta VALUES('frozen','provider usage exceeded approved reservation')")
            if status in ('succeeded', 'rejected'):
                db.execute("DELETE FROM meta WHERE key='connect_retry_required' AND value=?", (request_id,))

    def authorize_connect_retry(self, old_request_id, new_request_id, content_hash, proof):
        """One explicit new request after proven connection setup failure.

        Original UNKNOWN/count/token/CNY reservation is deliberately unchanged.
        This is not ReadTimeout/disconnect recovery and performs no transport.
        """
        self._require_time()
        if self.authority.get('operation_policy'):raise PermissionError('readonly closeout never retries an UNKNOWN step under a new identity')
        if not new_request_id or old_request_id == new_request_id:
            raise ValueError('connection retry requires a new request identity')
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM requests WHERE request_id=?', (old_request_id,)).fetchone()
            if old is None or old['status'] != 'unknown' or old['launched_at'] is None or old['content_hash'] != content_hash:
                raise ValueError('connection retry lacks exact original UNKNOWN request')
            existing = db.execute('SELECT * FROM connect_retries WHERE old_request_id=?', (old_request_id,)).fetchone()
            if existing:
                if existing['new_request_id'] != new_request_id or existing['content_hash'] != content_hash:
                    raise ValueError('connection failure already has its one explicit replacement')
                return json.loads(existing['proof_json'])
            if db.execute('SELECT 1 FROM requests WHERE request_id=?', (new_request_id,)).fetchone():
                raise ValueError('replacement request identity is already recorded')
            unknowns = {row[0] for row in db.execute("SELECT request_id FROM requests WHERE status IN ('unknown','reserved','dispatched')")}
            frozen = db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()
            if unknowns != {old_request_id} or not frozen or frozen[0] != 'unknown product HTTP; usage reservation retained':
                raise RuntimeError('unrelated uncertainty/freeze cannot be bypassed by connection retry')
            proof = dict(proof, original_request_row_sha256=sha_value(dict(old)), original_freeze_retained_in_audit=frozen[0],
                         old_request_id=old_request_id, new_request_id=new_request_id, new_http_requests_at_authorization=0)
            db.execute('INSERT INTO connect_retries VALUES(?,?,?,?,?,NULL)', (old_request_id, new_request_id, content_hash, json.dumps(proof, ensure_ascii=False), utc_now()))
            db.execute("DELETE FROM meta WHERE key='frozen' AND value='unknown product HTTP; usage reservation retained'")
            db.execute("INSERT INTO meta VALUES('connect_retry_required',?)", (new_request_id,))
            return proof

    def recover_http(self, request_id, content_hash, response, tokens, output, cost, proof):
        """Reconcile one already-dispatched complete reply, never dispatch HTTP.

        Preserve the interrupted original row in an append-only recovery table.
        Other unknown requests and unrelated freezes are never cleared.
        """
        authority_for(load_request(self.task_context['request_id'])) if self.task_context else authorization()
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if old is None or old['content_hash'] != content_hash or old['launched_at'] is None:
                raise ValueError('recovery lacks the exact original dispatched request')
            if old['status'] == 'succeeded':
                if json.loads(old['response']) != response:
                    raise ValueError('recovery conflicts with a recorded complete response')
                return
            if old['status'] not in ('reserved', 'dispatched', 'unknown'):
                raise ValueError('only interrupted/unknown requests permit complete-response recovery')
            if tokens > old['reserve_tokens'] or output > old['reserve_output'] or cost > old['reserve_cost']:
                raise ValueError('recovered usage exceeds the original reservation')
            db.execute('INSERT INTO http_recoveries VALUES(?,?,?,?)', (request_id, json.dumps(dict(old), ensure_ascii=False), json.dumps(proof, ensure_ascii=False), utc_now()))
            db.execute("UPDATE requests SET status='succeeded',actual_tokens=?,actual_output=?,actual_cost=?,response=?,reason=?,finished_at=? WHERE request_id=?", (tokens, output, cost, json.dumps(response, ensure_ascii=False), 'complete saved response reconciled; zero new HTTP', utc_now(), request_id))
            if not db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','dispatched','reserved')").fetchone():
                db.execute("DELETE FROM meta WHERE key='frozen' AND value='unknown product HTTP; usage reservation retained'")

    def reserve_process(self, kind, detail):
        if kind not in ('compile', 'simulation', 'worker', 'version') or not isinstance(detail, dict):
            raise ValueError('invalid V12 process kind/detail')
        self._require_time()
        if self.task_context: require_execution(self.task_context['request_id'])
        attempt_id = kind + '_' + uuid.uuid4().hex
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            self._blocked(db)
            if db.execute("SELECT 1 FROM process_attempts WHERE state='UNKNOWN'").fetchone():
                raise RuntimeError('unknown process requires readback; no new launch')
            # Worker naturally encloses its owned compile/simulation processes.
            if db.execute("SELECT 1 FROM process_attempts WHERE kind=? AND state IN ('RESERVED','STARTED')", (kind,)).fetchone():
                raise RuntimeError('same process kind already reserved/in flight')
            cap = self.authority['caps'].get(kind)
            count = db.execute("SELECT count(*) FROM process_attempts WHERE kind=? AND (launched_at IS NOT NULL OR state='RESERVED')", (kind,)).fetchone()[0]
            if cap is not None and count >= cap:
                raise RuntimeError('V12 ' + kind + ' launch quota reached')
            db.execute('INSERT INTO process_attempts VALUES(?,?,?,?,?,NULL,NULL,NULL,NULL)', (attempt_id, kind, json.dumps(detail, ensure_ascii=False), 'RESERVED', utc_now()))
        return attempt_id

    def launch_process(self, attempt_id, pid=None):
        # Call after Popen succeeds: failed Popen is explicitly NOT_LAUNCHED.
        with connection(self.path) as db:
            changed = db.execute("UPDATE process_attempts SET state='STARTED',launched_at=?,pid=? WHERE attempt_id=? AND state='RESERVED'", (utc_now(), pid, attempt_id)).rowcount
            if changed != 1:
                raise RuntimeError('process has no unique live reservation')
        return attempt_id

    def finish_process(self, attempt_id, result):
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM process_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if old is None:
                raise RuntimeError('process receipt cannot replace a final state')
            if old['state'] not in ('RESERVED', 'STARTED'):
                if old['result_json'] is not None and json.loads(old['result_json'])==result:
                    return dict(attempt_id=attempt_id,state=old['state'],reused=True)
                raise RuntimeError('process receipt cannot replace a final state')
            if result.get('schema')=='v12-owned-worker-process-exit-1':
                detail=json.loads(old['detail_json'])
                if (old['kind']!='worker' or result.get('attempt_id')!=attempt_id
                        or result.get('task_id')!=detail.get('task_id')
                        or result.get('owner')!=detail.get('owner')
                        or result.get('pid')!=old['pid']):
                    raise ValueError('owned worker receipt does not match its launched reservation')
            if old['launched_at'] is None:
                state = 'NOT_LAUNCHED'
            else:
                state = 'UNKNOWN' if result.get('uncertain') else 'FINISHED'
            db.execute('UPDATE process_attempts SET state=?,finished_at=?,result_json=? WHERE attempt_id=?', (state, utc_now(), json.dumps(result, ensure_ascii=False), attempt_id))
        return dict(attempt_id=attempt_id, state=state)

    def consume(self, kind, event_id, detail=None):
        caps = {'candidate': 'candidates', 'software_repair': 'software_repair_rounds', 'parameter_revision': 'parameter_revision_rounds', 'observation_extension': 'observation_extensions','source_revision':'maximum_complete_affected_scenario_rerun_rounds'}
        if kind not in caps:
            raise ValueError('unknown bounded event')
        self._require_time()
        if self.task_context:
            # Replaying a saved, already confirmed candidate can have exactly
            # zero HTTP permission. It still needs the full execution guard;
            # other counters retain their original planning permission path.
            if kind=='candidate' and self.authority.get('permissions',{}).get('planning_http') is not True:
                require_execution(self.task_context['request_id'])
            else:require_planning(self.task_context['request_id'])
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT detail_json FROM counters WHERE kind=? AND event_id=?', (kind, event_id)).fetchone()
            body = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
            if old:
                if old[0] != body:
                    raise ValueError('bounded event identity changed')
                return
            allocation=self.authority.get('repair_allocation')
            if allocation and kind=='software_repair':
                self._blocked(db)
                valid=db.execute("SELECT value FROM meta WHERE key='first_valid_generated_source'").fetchone()
                used=db.execute("SELECT count(*) FROM counters WHERE kind='software_repair'").fetchone()[0]
                if not valid and used>=allocation['maximum_before_first_valid_generated_source']:
                    raise RuntimeError('V12 reserves at least two software repairs for post-generation/runtime')
            cap=(self.authority.get('repair_allocation',{}).get(caps[kind],0) if kind=='source_revision' else self.authority['caps'][caps[kind]])
            if db.execute('SELECT count(*) FROM counters WHERE kind=?', (kind,)).fetchone()[0] >= cap:
                raise RuntimeError('V12 ' + kind + ' allowance exhausted')
            db.execute('INSERT INTO counters VALUES(?,?,?,?)', (kind, event_id, body, utc_now()))

    def mark_valid_generated_source(self,spec_digest):
        """A locally valid exact reviewed product source unlocks reserved repairs."""
        if not self.authority.get('repair_allocation'):return
        self._require_time()
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE');self._blocked(db)
            db.execute("INSERT OR IGNORE INTO meta VALUES('first_valid_generated_source',?)",(spec_digest,))

    def usage(self):
        with connection(self.path) as db:
            totals = db.execute("SELECT count(*),coalesce(sum(coalesce(actual_tokens,reserve_tokens)),0),coalesce(sum(coalesce(actual_output,reserve_output)),0),coalesce(sum(coalesce(actual_cost,reserve_cost)),0),coalesce(sum(actual_cost),0) FROM requests WHERE status<>'not_dispatched'").fetchone()
            actual_http = db.execute('SELECT count(*) FROM requests WHERE launched_at IS NOT NULL').fetchone()[0]
            unknown = db.execute("SELECT count(*) FROM requests WHERE status IN ('unknown','dispatched','reserved')").fetchone()[0]
            processes = {row[0]: row[1] for row in db.execute('SELECT kind,count(*) FROM process_attempts WHERE launched_at IS NOT NULL GROUP BY kind')}
            counters = {row[0]: row[1] for row in db.execute('SELECT kind,count(*) FROM counters GROUP BY kind')}
            frozen = db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()
            pending = db.execute("SELECT value FROM meta WHERE key='connect_retry_required'").fetchone()
        return dict(requests=actual_http, reserved_or_actual_requests=totals[0], tokens=totals[1], output_tokens=totals[2], cost_cny_reserved_or_actual=totals[3], known_actual_cost_cny=totals[4], unknown_requests=unknown, actual_cost_cny='UNKNOWN' if unknown else totals[4], processes=processes, counters=counters, frozen=frozen[0] if frozen else None, connect_retry_pending=pending[0] if pending else None)
