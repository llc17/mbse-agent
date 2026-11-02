"""Real MCP protocol over the same source/version/execution service."""
from week11.src.mcp_server import create_mcp
from .case_session import open_service
import sys,time,json,os
from .case_diagnostics import phase

def baseline_warmup_source(task_context,project_root):
    """The exact cold-start selection path, callable without starting MCP."""
    if task_context:
        from .case_taskflow import baseline_sysml_for_context
        return baseline_sysml_for_context(task_context)
    from .case_paths import read_json
    return read_json(project_root/'imported-reference.json')['frozen_spec']['artifact']['sysml_source']

def trace_method(service,name):
    original=getattr(service,name)
    def observed(*args,**kwargs):
        with phase(name):return original(*args,**kwargs)
    setattr(service,name,observed)


def create_case_mcp(service):
    """Extend the inherited protocol without changing its verified tools."""
    server=create_mcp(service)
    @server.tool(name='execution_progress',description='Read lifecycle and saved stage facts without CSV or evidence verification.')
    def execution_progress(task_id:str)->dict:
        return service.execution_progress(task_id)
    @server.tool(name='versions_index',description='List recorded execution identities and historical conclusions; not current evidence verification.')
    def versions_index()->dict:
        return service.versions_index()
    @server.tool(name='project_overview',description='Read concise project lifecycle metadata without whole-history verification.')
    def project_overview()->dict:
        return service.project_overview()
    return server

def main():
    service=open_service(with_worker=True)
    # Installed SysMLPy imports Pint/NumPy. The captured MCP worker stack
    # blocked inside NumPy's first C-extension import under the serving loop.
    # Load that unchanged dependency in the process main thread before stdio
    # starts its AnyIO threads; each proposal still runs all ordinary checks.
    with phase('SYSML_DEPENDENCY_PRELOAD'):
        import sysmlpy.antlr_parser
        from .case_paths import PROJECT_ROOT,TASK_CONTEXT,read_json
        from .case_toolchain import check_sysml
        warmup=check_sysml(baseline_warmup_source(TASK_CONTEXT,PROJECT_ROOT))
        if warmup.get('status')!='PASS':raise ValueError('existing imported SysML failed the ordinary parser during main-thread initialization')
    if os.environ.get('V12_CASE_MCP_VALIDATE_ONLY')=='1':
        def disabled_insert(record):
            print(json.dumps(dict(mcp_phase='INSERT_DISABLED_VALIDATION_COMPLETE',spec_digest=record.get('spec_digest'))),file=sys.stderr,flush=True)
            raise ValueError('DIAGNOSTIC_VALIDATION_COMPLETE_INSERT_DISABLED')
        service.store.insert_proposal=disabled_insert
        from .case_backend import module
        artifacts=module('artifact_service');original=artifacts.validate_artifact
        def traced_validate(*args,**kwargs):
            with phase('VALIDATE'):return original(*args,**kwargs)
        artifacts.validate_artifact=traced_validate
    for name in ('proposal_prepare','proposal_get','evidence_query','execution_start','execution_status'):trace_method(service,name)
    print(json.dumps(dict(mcp_phase='SERVER_READY_FOR_STDIO')),file=sys.stderr,flush=True)
    try:create_case_mcp(service).run('stdio')
    finally:service.worker_manager.shutdown()

if __name__=='__main__':main()
