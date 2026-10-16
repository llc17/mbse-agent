"""Single known all-thinking generation recovery; original run and failure survive."""
import hashlib, json, math, sqlite3
from .contracts import inside_week12
from .model_artifact import save
from week11.src.runtime import RuntimeError, _connection, _digest, _estimate_input, _ALLOWED_RESPONSE_MODELS


class GenerationLengthRecovery:
    def _load_generation_recovery(self):
        with _connection(self.ledger_path) as db:
            row=db.execute("SELECT value FROM meta WHERE key='generation_length_recovery'").fetchone()
        self._generation_length_recovery=json.loads(row[0]) if row else None
        if self._generation_length_recovery:self._check_generation_recovery()

    def _check_generation_recovery(self, *, before_submit=False):
        from .artifact_session import digest
        record=self._generation_length_recovery
        if not record:return
        root=self.ledger_path.parent
        if record.get('schema')!='v11-generation-length-recovery-1' or record.get('config_hash')!=self._config_hash or record.get('reasoning_effort')!='low' or record.get('maximum_new_requests')!=1 or record.get('physical_approval') is not False or json.loads((root/'generation-length-recovery.json').read_text(encoding='utf8'))!=record:raise RuntimeError('generation recovery binding differs')
        for path,sha in record['unchanged_file_sha256'].items():
            if digest(inside_week12(path))!=sha:raise RuntimeError('generation recovery source/approval/context drift')
        with _connection(self.ledger_path) as db:
            state=db.execute("SELECT value FROM meta WHERE key='generation_length_recovery_state'").fetchone()
            rows=self._recovery_request_rows(db)
        if state not in [('pending',),('submitted',),('completed',)] or rows[:len(record['original_request_rows'])]!=record['original_request_rows']:raise RuntimeError('generation recovery original rows/state differ')
        if before_submit:
            with sqlite3.connect((root/'projects.sqlite').as_uri()+'?mode=ro',uri=True) as db:
                if any(db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('proposals','tasks')):raise RuntimeError('generation recovery forbidden after dispatch')
                sources=[dict(id=r[0],text=r[1]) for r in db.execute('SELECT source_id,text FROM sources WHERE session_id=? AND project_id=? ORDER BY rowid',(record['session_id'],record['project_id']))]
            if sources!=record['sources'] or any((root/'cli-turns').glob('*-proposal-dispatch.json')) or any((root/'cli-turns').glob('*-tool-results.json')):raise RuntimeError('generation recovery sources/dispatch differ')

    def prepare_known_generation_length_recovery(self,manifest_path,*,authorization_text):
        from .mcp_adapter import exclusive_server_lock
        from .artifact_session import now,digest
        from .artifact_continuous import digest as value_digest,validate_commitment
        root=self.ledger_path.parent;manifest_path=inside_week12(manifest_path)
        with exclusive_server_lock(root/'projects.sqlite'):
            if self._generation_length_recovery or self._transport_recovery or self.config.get('budget_profile')!='interactive-20261013' or self.config['thinking']!='enabled' or manifest_path!=root/'run.json' or self.config_path!=root/'runtime.json' or not authorization_text.strip():raise RuntimeError('original enabled interactive generation required; recovery cannot renew')
            manifest=json.loads(manifest_path.read_text(encoding='utf8'))
            if manifest.get('status')!='ACTIVE' or manifest.get('mode')!='LIVE' or not manifest.get('quota_confirmation') or manifest['runtime_config_sha256']!=digest(self.config_path) or manifest['context_sha256']!=digest(root/'context.json'):raise RuntimeError('generation recovery run drift or closed')
            pointer_path=root/'cli-continuations'/('current-'+hashlib.sha256(manifest['session'].encode()).hexdigest()+'.json');pointer=json.loads(pointer_path.read_text(encoding='utf8'))
            design_path=root/'cli-continuations'/(pointer['design_id']+'.json');design=json.loads(design_path.read_text(encoding='utf8'));did=design['design_id']
            if design.get('state')!='READY' or did!='flow_'+design['displayed_digest'] or design['displayed_digest']!=value_digest({k:v for k,v in design.items() if k not in {'displayed_digest','design_id'}}):raise RuntimeError('valid READY design required')
            approval_path=root/'cli-continuations'/(did+'-approval.json');approval=json.loads(approval_path.read_text(encoding='utf8'))
            expected=dict(schema='v11-continuous-design-approval-1',design_id=did,displayed_digest=design['displayed_digest'],session_id=manifest['session'],project_id=design['project_id'],source_snapshot=design['sources'],budget=design['budget'],channel='trusted_local_design_delegation',source_reviewed_by_human=False,scope='generate and execute only a candidate matching the confirmed finite commitment')
            if approval.get('approval_text') not in {'确认','批准','同意','CONFIRM'} or {k:v for k,v in approval.items() if k!='approval_text'}!=expected or (root/'cli-continuations'/(did+'-declined.json')).exists() or (root/'cli-continuations'/(did+'-execution.json')).exists():raise RuntimeError('generation approval drift or execution attempted')
            if pointer['sources']!=design['sources'] or pointer['project_id']!=design['project_id'] or design['budget']['runtime_config']!=dict(self.config):raise RuntimeError('generation current design/source/budget drift')
            validate_commitment(design['commitment'],design['sources'])
            oldrid='flow_generate_'+design['displayed_digest'];rid=oldrid+'_length_low1'
            request_path=root/'cli-turns'/(hashlib.sha256(oldrid.encode()).hexdigest()+'-request.json');request=json.loads(request_path.read_text(encoding='utf8'))
            context=json.loads(request['messages'][1]['content']);approved=context.get('approved_design')
            if not request['tools'] or approved!=dict(body=design['body'],trusted_sources=design['sources'],commitment=design['commitment'],approval=approval) or context.get('trusted_sources')!=design['sources']:raise RuntimeError('original generation request approval/source differs')
            evidence_path=root/'provider-responses'/(_digest(oldrid)+'.json');evidence=json.loads(evidence_path.read_text(encoding='utf8'));response=evidence.get('response',{});usage=response.get('usage',{});choices=response.get('choices',[])
            if evidence.get('request_id')!=oldrid or evidence.get('request_thinking')!='enabled' or response.get('model') not in _ALLOWED_RESPONSE_MODELS or not response.get('id') or len(choices)!=1 or choices[0].get('finish_reason')!='length' or not isinstance(choices[0].get('message'),dict) or choices[0]['message'].get('content') not in ('',None) or choices[0]['message'].get('tool_calls'):raise RuntimeError('bound empty completed generation length required')
            if any(type(usage.get(k)) is not int or usage[k]<0 for k in ('prompt_tokens','completion_tokens','total_tokens')) or usage['total_tokens']!=usage['prompt_tokens']+usage['completion_tokens'] or not 0<usage['completion_tokens']<=self.config['max_tokens'] or usage.get('completion_tokens_details',{}).get('reasoning_tokens')!=usage['completion_tokens']:raise RuntimeError('generation all-thinking usage mismatch')
            estimated=_estimate_input(request['messages'],request['tools']);tokens=estimated+self.config['max_tokens'];cost=(estimated*self.config['input_usd_per_million']+self.config['max_tokens']*self.config['output_usd_per_million'])/1000000
            if estimated>self.config['max_input_tokens']:raise RuntimeError('generation input cap exceeded')
            audit=root/'generation-length-recovery.json'
            if audit.exists():raise RuntimeError('generation recovery already recorded')
            with _connection(self.ledger_path) as db:
                db.execute('BEGIN IMMEDIATE');rows=self._recovery_request_rows(db);old=next((r for r in rows if r['request_id']==oldrid),None);frozen=db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()
                expected_cost=(usage['prompt_tokens']*self.config['input_usd_per_million']+usage['completion_tokens']*self.config['output_usd_per_million'])/1000000
                if db.execute("SELECT 1 FROM meta WHERE key='generation_length_recovery'").fetchone() or db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()!=(self._config_hash,) or frozen!=('invalid response or budget overrun',) or not old or old['status']!='unknown' or old['reason']!=frozen[0] or old['content_hash']!=_digest(request) or old['actual_tokens']!=usage['total_tokens'] or not isinstance(old['actual_cost'],(int,float)) or not math.isclose(old['actual_cost'],expected_cost,abs_tol=1e-12,rel_tol=0) or usage['prompt_tokens']>old['reserve_tokens']-self.config['max_tokens']:raise RuntimeError('not bound known generation length freeze')
                # Earlier completed design recovery may leave a known unknown row.
                completed_design=db.execute("SELECT value FROM meta WHERE key='design_length_recovery_state'").fetchone()==('completed',)
                permitted=self._design_length_recovery['original_request_rows'] if completed_design and self._design_length_recovery else []
                if any(r is not old and r['status']!='succeeded' and r not in permitted for r in rows) or any(r['actual_tokens'] is None or r['actual_cost'] is None or r['request_id']==rid for r in rows):raise RuntimeError('unknown usage/other uncertain attempt blocks generation recovery')
                totals=dict(requests=len(rows),tokens=sum(r['actual_tokens'] for r in rows),cost=sum(r['actual_cost'] for r in rows))
                if totals['requests']>=self.config['max_requests'] or totals['tokens']+tokens>self.config['max_total_tokens'] or totals['cost']+cost>self.config['max_usd']:raise RuntimeError('existing caps cannot fit generation recovery')
                record=dict(schema='v11-generation-length-recovery-1',created_at=now(),authorization_text=authorization_text,authorization_channel='existing_run_repair_delegation',physical_approval=False,config_hash=self._config_hash,original_request_rows=rows,original_totals=totals,original_frozen=frozen[0],original_request_id=oldrid,new_request_id=rid,request_content_hash=_digest(dict(request,reasoning_effort='low')),reasoning_effort='low',maximum_new_requests=1,session_id=manifest['session'],project_id=design['project_id'],design_id=did,sources=design['sources'],unchanged_file_sha256={str(p):digest(p) for p in (manifest_path,self.config_path,root/'context.json',pointer_path,design_path,approval_path,request_path,evidence_path)},exclusive_db_lock_acquired=True)
                self._generation_length_recovery=record
                # Verify sources/empty dispatch before persisting the authorization.
                with sqlite3.connect((root/'projects.sqlite').as_uri()+'?mode=ro',uri=True) as projectdb:
                    sources=[dict(id=r[0],text=r[1]) for r in projectdb.execute('SELECT source_id,text FROM sources WHERE session_id=? AND project_id=? ORDER BY rowid',(manifest['session'],design['project_id']))]
                    if sources!=design['sources'] or any(projectdb.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('proposals','tasks')) or any((root/'cli-turns').glob('*-proposal-dispatch.json')) or any((root/'cli-turns').glob('*-tool-results.json')):raise RuntimeError('generation sources/dispatch drift')
                save(audit,record);db.execute("INSERT INTO meta(key,value) VALUES('generation_length_recovery',?)",(json.dumps(record,ensure_ascii=False),));db.execute("INSERT INTO meta(key,value) VALUES('generation_length_recovery_state','pending')")
            return record

    def generation_request_id(self,original_id):
        record=self._generation_length_recovery
        return record['new_request_id'] if record and original_id==record['original_request_id'] else original_id

    def generation_effort(self,request_id):
        record=self._generation_length_recovery
        return 'low' if record and request_id==record['new_request_id'] else None

    def generation_cached_resume_ready(self):
        """Only the same succeeded response, before any proposal/task dispatch."""
        record=self._generation_length_recovery
        if not record:return False
        try:
            self._check_generation_recovery(before_submit=True)
            root=self.ledger_path.parent;rid=record['new_request_id']
            with _connection(self.ledger_path) as db:
                if db.execute("SELECT value FROM meta WHERE key='generation_length_recovery_state'").fetchone()!=('submitted',) or db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()!=(record['original_frozen'],):return False
                row=next((r for r in self._recovery_request_rows(db) if r['request_id']==rid),None)
            if not row or row['status']!='succeeded' or row['content_hash']!=record['request_content_hash']:return False
            sent=json.loads((root/'cli-turns'/(hashlib.sha256(rid.encode()).hexdigest()+'-request.json')).read_text(encoding='utf8'))
            original=json.loads((root/'cli-turns'/(hashlib.sha256(record['original_request_id'].encode()).hexdigest()+'-request.json')).read_text(encoding='utf8'))
            evidence=json.loads((root/'provider-responses'/(_digest(rid)+'.json')).read_text(encoding='utf8'));cached=json.loads(row['response'])
            if sent!=original or _digest(dict(sent,reasoning_effort='low'))!=record['request_content_hash'] or evidence.get('request_id')!=rid or evidence.get('request_thinking')!='enabled' or evidence.get('request_reasoning_effort')!='low' or evidence.get('request_content_hash')!=record['request_content_hash']:return False
            from .artifact_runtime import parse_artifact_response
            parsed,input_tokens,output_tokens=parse_artifact_response(evidence['response'])
            expected_cost=(input_tokens*self.config['input_usd_per_million']+output_tokens*self.config['output_usd_per_million'])/1000000
            return bool(cached.get('response_id')==evidence['response']['id'] and all(cached.get(k)==parsed.get(k) for k in ('content','tool_calls')) and row['actual_tokens']==input_tokens+output_tokens and isinstance(row['actual_cost'],(int,float)) and math.isclose(row['actual_cost'],expected_cost,abs_tol=1e-12,rel_tol=0))
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):return False

    def _reserve_generation(self,request_id,content_hash,tokens,cost):
        record=self._generation_length_recovery
        if not record:return False
        with _connection(self.ledger_path) as db:
            state=db.execute("SELECT value FROM meta WHERE key='generation_length_recovery_state'").fetchone()
        if state==('completed',):return False
        self._check_generation_recovery(before_submit=True)
        with _connection(self.ledger_path) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT value FROM meta WHERE key='generation_length_recovery_state'").fetchone()!=('pending',) or request_id!=record['new_request_id'] or content_hash!=record['request_content_hash'] or self._recovery_request_rows(db)!=record['original_request_rows'] or db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()!=(record['original_frozen'],) or db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()!=(self._config_hash,):raise RuntimeError('only single bound generation recovery allowed; no further request')
            totals=record['original_totals']
            if totals['requests']>=self.config['max_requests'] or totals['tokens']+tokens>self.config['max_total_tokens'] or totals['cost']+cost>self.config['max_usd']:raise RuntimeError('runtime budget exhausted')
            db.execute('INSERT INTO requests(request_id,content_hash,status,reserve_tokens,reserve_cost) VALUES(?,?,?,?,?)',(request_id,content_hash,'reserved',tokens,cost));db.execute("UPDATE meta SET value='submitted' WHERE key='generation_length_recovery_state'")
        return True

    def finish_generation_recovery(self,request_id,proposal,commitment):
        record=self._generation_length_recovery
        if not record or request_id!=record['new_request_id']:return
        from .artifact_continuous import compare_candidate
        from .model_artifact import validate_artifact
        checked=validate_artifact(proposal['spec'],require_sysml=True)
        if not checked['ready'] or checked['spec_digest']!=proposal['spec_digest'] or checked['spec']['sources']!=record['sources']:raise RuntimeError('valid source-bound persisted proposal required')
        compare_candidate(commitment,checked['spec'])
        self._check_generation_recovery()
        with _connection(self.ledger_path) as db:
            db.execute('BEGIN IMMEDIATE')
            state=db.execute("SELECT value FROM meta WHERE key='generation_length_recovery_state'").fetchone()
            if state==('completed',):return
            if state!=('submitted',) or db.execute('SELECT status FROM requests WHERE request_id=?',(request_id,)).fetchone()!=('succeeded',) or db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()!=(record['original_frozen'],):raise RuntimeError('generation recovery has no validated candidate')
            db.execute("UPDATE meta SET value='completed' WHERE key='generation_length_recovery_state'");db.execute("INSERT INTO meta(key,value) VALUES('generation_length_recovery_proposal',?)",(proposal['proposal_id'],));db.execute("DELETE FROM meta WHERE key='frozen' AND value=?",(record['original_frozen'],))
