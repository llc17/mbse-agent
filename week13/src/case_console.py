"""Chinese interactive terminal over the existing, authorization-bound CLI.

Only this UI's selection/history is persisted here. All product operations use
fresh subprocesses so request selectors cannot leak through imported modules.
"""
import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
import queue
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .case_console_format import parameter_label, parameter_value, terminal_columns, wrapped_lines
from .case_console_result import prepare_result,concise_result_lines

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
CONSOLE_ROOT = ROOT / 'runs/console'
# The interpreter cannot import product modules or spawn their descendants
# until its parent has assigned the still-waiting process to the Windows Job.
CHILD_GATE = "import os,sys\nprint(os.getpid(),flush=True)\nif sys.stdin.buffer.read(1)!=b'1': raise SystemExit(125)\nsys.stdin.close()\nimport runpy\nsys.argv=sys.argv[1:]\nrunpy.run_module(sys.argv[0],run_name='__main__')\n"
CAP_LABELS = dict(http_requests='产品HTTP请求数', product_cny='人民币总上限', total_tokens='累计总tokens', total_output_tokens='累计输出tokens', compile='编译次数', simulation='仿真次数', candidates='候选次数', software_repair_rounds='软件修复轮数', parameter_revision_rounds='参数修订轮数', observation_extensions='观察延长次数')
STATUS = dict(PASS='通过', FAIL='失败', UNKNOWN='未知（不能当作成功）', NOT_RUN='未运行', NOT_VERIFIED='未验证', IMPORTED_REFERENCE_NOT_RUN='导入原始参考，未重新运行', IMPORTED_REFERENCE='导入原始参考，未重新运行', NEEDS_INPUT='缺少必要事实', UNSUPPORTED='超出当前支持范围', SUPPORTED='在当前支持范围内', PROPOSED='方案待审查', CONFIRMED='方案已确认', DRAFT='已记录原文', NO_EXECUTABLE_PLAN='没有可执行方案', AWAITING_CONFIRMATION_OR_EXECUTION_AUTHORITY='等待方案确认或执行授权', NO_TERMINAL_EXECUTION='本请求尚无终态运行结果')
NO_DATA_STATEMENTS = frozenset(('我现在提供不了数据', '现在提供不了数据', '暂时没有数据', '我暂时没有数据', '暂时无法提供数据'))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def listed_result(row):
    if row.get('verification_status')=='NOT_RECHECKED':
        return '已记录任务'+shown(row.get('recorded_task_status','UNKNOWN'))+'；本次未重新核验'
    return shown(row.get('status') or row.get('task_status','UNKNOWN'))


def write(path, value, *, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if immutable:
        with path.open('x', encoding='utf8') as handle:
            handle.write(body)
    else:
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(body, encoding='utf8')
        temporary.replace(path)


def shown(value):
    return STATUS.get(value, value) if isinstance(value, str) else str(value)


def intake_reason(reason):
    value = str(reason)
    marker = 'approved parameter is not uniquely declared: '
    if marker in value:
        name = value.split(marker, 1)[1].strip()
        return ('质量参数 ' if name == 'm' else '参数 ') + name + ' 未唯一声明，当前检查器不适用。'
    return value


def classify_error(error):
    """Keep independent limits distinct; the backend remains authoritative."""
    value = str(error).lower()
    if 'repeatedprotocolfailure' in value or '同类协议错误重复' in value:
        return 'protocol_repeat', '同类协议错误重复，已停止自动修复；没有新增修复扣数或产品调用。'
    if 'software_repair' in value and 'allowance exhausted' in value:
        return 'software_repair', '自动纠错额度已用尽；受影响的生成动作停止。'
    if any(word in value for word in ('wall-clock', 'deadline', 'hard time', 'budget expired')):
        return 'time', '硬总截止已到；停止新增执行。'
    if 'read_idle_timeout' in value:
        return 'read_timeout','等待响应数据超时；远端是否已收到及计费仍未知，不能自动重发。'
    if 'connect_timeout' in value or 'connect_error' in value:
        return 'connection','连接服务失败；请核对连接阶段证据。'
    if 'stream_interrupted' in value:
        return 'stream_interrupted','响应流中途断开，半截回复不能作为完整方案。'
    if 'no_substantive_progress_timeout' in value:
        return 'no_progress','无实质进展时限已到；心跳不计进展。'
    if any(word in value for word in ('unknown', 'uncertain', 'in-flight', 'in flight', 'http_child_transport_error', 'http_child_incomplete', 'http_child_exit_unconfirmed')):
        return 'unknown', '结果未知或仍在途；先读回，不能自动重发。'
    if 'frozen' in value:
        return 'frozen', '账本已冻结；停止受限动作并读回原因。'
    if 'protects required explanation/reopen http slots' in value:
        return 'http_reserved', '剩余HTTP槽位须保留给解释和重开；本次新增调用未获放行。'
    if 'http/tokens/cny quota reached' in value:
        return 'product_quota', '产品调用额度不足；请核对HTTP、费用、tokens和单次预留，不能据此认定各项都已用满。'
    quota = any(word in value for word in ('quota reached', 'allowance exhausted', 'cap exceeded', 'limit exceeded', 'budget exhausted', 'budget exceeded'))
    if quota and any(word in value for word in ('product_cny', 'cost', 'money', 'cny')):
        return 'money', '产品费用额度不足；停止新增产品调用。'
    if quota and 'token' in value:
        return 'tokens', 'tokens额度不足；停止新增产品调用。'
    if quota and any(word in value for word in ('http', 'request allowance', 'request cap')):
        return 'http', '产品HTTP次数额度不足；停止新增产品调用。'
    if 'allowance exhausted' in value:
        return 'action_limit', '当前动作对应的次数额度已用尽；其他额度分别判断。'
    return 'action_error', '动作未完成；已保留原始证据，不会自动重试。'


class ActionError(Exception):
    def __init__(self, message, evidence=None, *, kind=None):
        super().__init__(message)
        self.evidence = evidence
        self.kind = kind


class ActionInterrupted(ActionError):
    pass


class WindowsJob:
    """Kill the complete child process tree on cancellation or owner exit."""
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes
        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(key, ctypes.c_uint64) for key in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        actual_handle = None
        failure_stage = 'job_limit_setup'
        okay = bool(handle and kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)))
        if okay and getattr(process, 'actual_interpreter_pid', process.pid) != process.pid:
            # Windows venv redirectors can create the real interpreter before
            # Popen returns. That interpreter is still blocked at CHILD_GATE.
            actual_handle = kernel.OpenProcess(0x0100 | 0x0001 | 0x0400, False, process.actual_interpreter_pid)
            in_job = wintypes.BOOL()
            failure_stage = 'actual_interpreter_job_assignment'
            okay = bool(actual_handle and kernel.IsProcessInJob(actual_handle, handle, ctypes.byref(in_job)) and (in_job.value or kernel.AssignProcessToJobObject(handle, actual_handle)))
        elif okay:
            failure_stage = 'launcher_job_assignment'
            okay = bool(kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)))
        if not okay:
            error_code = ctypes.get_last_error()
            if actual_handle:
                kernel.TerminateProcess(actual_handle, 125)
                kernel.CloseHandle(actual_handle)
            if handle:
                kernel.CloseHandle(handle)
            if process.stdin:
                process.stdin.close()
            process.kill()
            process.wait()
            raise OSError(error_code, '无法建立完整子进程退出保护：' + failure_stage + '；动作没有继续')
        if actual_handle:
            kernel.CloseHandle(actual_handle)
        self.kernel, self.handle = kernel, handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class CliRunner:
    def __init__(self, console_root=CONSOLE_ROOT, output=print):
        self.root, self.output = Path(console_root), output

    def call(self, action, **options):
        return self._run('week13.src.case_cli', [action] + self._options(options), action)

    def data(self, action, **options):
        return self._run('week13.src.case_console_data', [action] + self._options(options), action)

    def projects(self, action, **options):
        return self._run('week13.src.case_projects_cli', [action] + self._options(options), 'case_' + action)

    @staticmethod
    def _options(options):
        result = []
        for key, value in options.items():
            if value is None or value is False:
                continue
            result.append('--' + key.replace('_', '-'))
            if value is not True:
                result.append(json.dumps(value, ensure_ascii=False, separators=(',', ':')) if isinstance(value, (dict, list)) else str(value))
        return result

    def _run(self, module, args, label):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        env.pop('MBSE_V12_REQUEST_ID', None)
        command = [sys.executable, '-X', 'utf8', '-B', '-c', CHILD_GATE, module] + args
        flags = dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else dict(start_new_session=True)
        operation = self.root / 'operations' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12])
        operation.parent.mkdir(parents=True, exist_ok=True)
        progress_file = operation.with_suffix('.progress.json')
        env['MBSE_V12_PROGRESS_FILE'] = str(progress_file.resolve())
        started = time.monotonic()
        last_progress, progress_key = None, None
        published_results=set()
        process = subprocess.Popen(command, cwd=REPO, env=env, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf8', errors='replace', **flags)
        stdout, stderr = '', ''
        job = None
        cancelled = False
        setup_error = None
        try:
            ready = queue.Queue(maxsize=1)
            def read_ready():
                try:
                    ready.put(process.stdout.readline())
                except (ValueError, OSError):
                    ready.put('')
            threading.Thread(target=read_ready, daemon=True).start()
            try:
                pid_line = ready.get(timeout=10).strip()
            except queue.Empty:
                raise ActionError('本地解释器10秒内未就绪，未放行任何产品动作。') from None
            if not pid_line.isdecimal() or not 0 < int(pid_line) < 2**32:
                raise ActionError('本地解释器身份握手失败，未放行任何产品动作。')
            process.actual_interpreter_pid = int(pid_line)
            job = WindowsJob(process)
            process.stdin.write('1')
            process.stdin.flush()
            process.stdin.close()
            process.stdin = None
            next_notice = time.monotonic() + 10
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    try:
                        progress = read(progress_file)
                        if progress.get('schema') == 'v12-operation-progress-1' and isinstance(progress.get('stage'), str) and isinstance(progress.get('message'), str):
                            last_progress = progress
                            key = (progress['stage'], progress['message'])
                            if key != progress_key:
                                self.output(f"阶段：{progress['message']}；已用 {time.monotonic() - started:.0f} 秒" + self.progress_counts(progress))
                                progress_key = key
                                next_notice = time.monotonic() + 30
                            pointer=(progress.get('details') or {}).get('result_ready') or {}
                            notified=tuple(pointer.get(field) for field in ('request_id','project_id','task_id','version_id','sha256'))
                            early=self.early_result(progress,args) if pointer and notified not in published_results else None
                            if early and early[0] not in published_results:
                                for line in early[1]:self.output(line)
                                self.output('任务结论已取得；结果说明仍在处理，失败或未通过核对不改变上述结论。')
                                published_results.add(early[0])
                    except (OSError, ValueError, TypeError, AttributeError):
                        pass  # Missing/partial evidence never invents a stage.
                    if time.monotonic() >= next_notice:
                        stage = last_progress['message'] if last_progress else '当前动作（尚无阶段回执）'
                        self.output(f"当前阶段：{stage}；已用 {time.monotonic() - started:.0f} 秒；该阶段尚无新完成回执" + self.progress_counts(last_progress) + '。提示不计为新HTTP；Ctrl+C 可中止。')
                        next_notice = time.monotonic() + 30
        except KeyboardInterrupt:
            cancelled = True
            if job:
                job.close()
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=10)
        except (OSError, ActionError) as exc:
            setup_error = exc
            if process.stdin:
                process.stdin.close()
                process.stdin = None
            if job:
                job.close()
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=10)
        finally:
            if job:
                job.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        write(operation.with_suffix('.json'), dict(stage=label, returncode=process.returncode, interrupted=cancelled, local_cli_exited=process.poll() is not None, launcher_pid=process.pid, actual_interpreter_pid=getattr(process, 'actual_interpreter_pid', None), ownership_established_before_module_dispatch=job is not None, owned_descendants_termination_requested=job is not None, remote_cancellation_proven=False if cancelled else None, setup_error=str(setup_error) if setup_error else None, progress_file=str(progress_file), last_progress=last_progress, elapsed_s=time.monotonic() - started, stdout=stdout, stderr=stderr), immutable=True)
        if setup_error:
            raise ActionError('本地进程保护未就绪，未放行产品动作。', str(operation.with_suffix('.json')))
        if cancelled:
            raise ActionInterrupted('已中止本地动作，远端或已写入的结果可能未知；请用 /status 和 /result 读回，不自动重发。', str(operation.with_suffix('.json')))
        try:
            result = json.loads(stdout)
        except (ValueError, TypeError):
            raise ActionError('子命令未返回完整结果；已保留证据，请先只读检查。', str(operation.with_suffix('.json'))) from None
        if process.returncode or 'error' in result:
            error = result.get('error', '')
            kind, message = classify_error(error)
            details=result.get('transport_failure') or {}
            if details:
                phase={'WAITING_FOR_RESPONSE':'等待响应','READING_RESPONSE':'读取响应流','CONNECTING':'建立连接','HTTP_TRANSPORT':'HTTP通信'}.get(details.get('phase'),'HTTP通信')
                message+='\n阶段：'+phase+'；完整回复：'+('已保存，请先零HTTP恢复' if details.get('saved_complete_response') else '未保存，当前没有完整可用回复')
                if details.get('affected_stage')=='plan':message+='\n本次失败方案请求未触发模型生成、编译或仿真。'
                message+='\n恢复入口：'+str(details.get('recovery_entry','/recover response'))
            raise ActionError(message + '\n原始原因：' + str(error), str(operation.with_suffix('.json')), kind=kind)
        return result

    @staticmethod
    def early_result(progress,args):
        """Only this operation's bound small terminal receipt can be shown.

        Progress is a notification, never the result authority. Re-read and
        bind its immutable receipt; no project/history/CSV query is made.
        """
        try:
            if '--request-id' not in args:return None
            expected=args[args.index('--request-id')+1]
            details=progress.get('details') or {};pointer=details.get('result_ready') or {}
            if details.get('request_id')!=expected or pointer.get('request_id')!=expected:return None
            path=Path(pointer['path']);resolved=path.resolve()
            if path!=resolved or not resolved.is_relative_to((ROOT/'runs').resolve()) or resolved.name!='pipeline-partial.json' or resolved.parent.name!=expected:return None
            raw=resolved.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=pointer.get('sha256'):return None
            result=json.loads(raw);state=result.get('state') or {};latest=state.get('latest_request_execution') or {}
            if result.get('request_id')!=expected or state.get('request_id')!=expected or state.get('project_id')!=pointer.get('project_id'):return None
            if any(not pointer.get(key) or latest.get(key)!=pointer[key] for key in ('project_id','task_id','version_id')):return None
            if latest.get('state') not in ('succeeded','failed','cancelled','uncertain'):return None
            view,execution,reviewed,headlines=prepare_result(result,request_id=expected)
            if reviewed or view.get('result_binding_conflict') or not execution:return None
            identity=tuple(pointer[key] for key in ('request_id','project_id','task_id','version_id','sha256'))
            return identity,concise_result_lines(view,execution,False,headlines,evidence_root=str(resolved.parent))
        except (OSError,ValueError,TypeError,KeyError,IndexError):return None

    @staticmethod
    def progress_counts(progress):
        details = (progress or {}).get('details') or {}
        used = details.get('http_requests')
        cap = details.get('http_requests_cap')
        return f'；产品HTTP {used}/{cap}' if used is not None and cap is not None else ''


class Console:
    def __init__(self, runner=None, *, console_root=CONSOLE_ROOT, input_fn=input, output=print, execution_timeouts=None, delegated_approval_basis=None):
        self.root = Path(console_root)
        self.output, self.input = output, input_fn
        self.runner = runner or CliRunner(self.root, output)
        self.execution_timeouts = execution_timeouts
        self.delegated_approval_basis = delegated_approval_basis
        if delegated_approval_basis is not None and (not isinstance(delegated_approval_basis, str) or not delegated_approval_basis.strip() or not Path(delegated_approval_basis).is_file()):
            raise ValueError('委托确认必须引用真实已有的授权记录文件')
        self.selection_path = self.root / 'selection.json'
        stored = read(self.selection_path) if self.selection_path.exists() else {}
        self.state = stored if stored.get('schema_version') == 2 else dict(schema_version=2, case_id=None, cases={})
        # Saved selection is history until the user explicitly opens/resumes.
        self.legacy_selection = dict(stored) if stored and stored.get('schema_version') != 2 else None
        self.selection = dict(request_id=None, baseline_version_id=None)
        self.catalog = None
        self.ctx = None
        self.quit = False

    def save(self):
        case_id = self.catalog.get('case_id') if self.catalog else None
        if not case_id:
            return
        self.selection['input_mode'] = self.mode()
        if case_id:
            self.state['case_id'] = case_id
            self.state['cases'][case_id] = dict(self.selection)
        write(self.selection_path, self.state)

    def history(self, kind, **value):
        write(self.root / 'history' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex + '.json'), dict(kind=kind, at_utc=datetime.now(timezone.utc).isoformat(), **value), immutable=True)

    def missing_input(self):
        ctx = self.ctx or {}
        plan = ctx.get('plan') or {}
        actual = ctx.get('execution_state') or ((ctx.get('stage_facts') or {}).get('current') or {}).get('execution_state')
        return plan.get('support') == 'NEEDS_INPUT' and bool(plan.get('missing')) and ctx.get('access_mode') != 'RESULT_REVIEW_ONLY' and not ctx.get('terminal_saved') and actual == 'NOT_RUN'

    def defer_binding(self):
        ctx = self.ctx or {}
        plan_digest = hashlib.sha256(json.dumps(ctx.get('plan'), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()
        return dict(project_id=ctx.get('project_id'), request_id=ctx.get('request_id'), baseline_version_id=ctx.get('baseline_version_id'), plan_digest=ctx.get('displayed_digest') or plan_digest, plan_sha256=plan_digest)

    def show_missing_options(self, emit=None):
        if not self.missing_input():
            return
        emit = emit or self.output
        record = self.selection.get('deferred_input') or {}
        if record.get('binding') == self.defer_binding():
            emit('已记录资料暂缺：' + record['raw_text'] + '（UTC ' + record['at_utc'] + '）。缺项仍未解决，方案不可执行；这不是执行批准。')
        else:
            emit('资料暂缺时可输入 /defer 原因，仅保存本地备注；缺项仍未解决，不会生成或执行。')
        emit('可用 /ask 问题 询问缺项影响；只读产品咨询可能消耗原预算HTTP，仍受原截止和上限约束。/status 可查看已有本地状态。')
        emit('可以咨询假设及其影响；资料暂缺不会自动填值，也不代表已批准按假设执行。')
        emit('也可用 /new 提出受支持的继承模型诊断；不会自动切换，原模型的已知缺陷仍会如实报告。有数据后直接补充事实，沿同账及原截止。')

    def defer(self, text):
        self.refresh()
        ctx = self.ctx or {}
        if not self.missing_input():
            self.output('仅尚未运行且明确缺少关键事实的请求可记录资料暂缺；未改变方案或执行状态。')
            return
        if not text.strip():
            self.output('用法：/defer 资料暂缺的原因；仅保存本地备注，不解决缺项或批准执行。')
            return
        binding = self.defer_binding()
        previous = self.selection.get('deferred_input') or {}
        if previous.get('binding') != binding or previous.get('raw_text') != text:
            record = dict(binding=binding, raw_text=text, missing=list(ctx['plan']['missing']), at_utc=datetime.now(timezone.utc).isoformat())
            self.history('input_deferred', deferral=record)
            self.selection['deferred_input'] = record
            self.save()
        self.show_missing_options()

    def refresh(self):
        ctx = self.runner.data('context', request_id=self.selection['request_id']) if self.selection.get('request_id') else None
        if ctx and self.catalog and ctx['project_id'] != self.catalog['project_id']:
            self.ctx = None
            raise ActionError('请求属于其他案例，未载入；用 /case open 切换案例，或 /open ID 明确打开其所属案例。')
        self.ctx = ctx
        return self.ctx

    def start(self):
        self.catalog, self.ctx = None, None
        self.selection = dict(request_id=None, baseline_version_id=None)
        self.show_home()

    def show_home(self):
        self.output('V12 中文交互终端。')
        self.output('首页：请选择案例后开始；已有案例直接打开即可，无需再次导入。')
        self.output('/case import "资料目录"　导入新案例')
        self.output('/case list　列出已有案例')
        self.output('/case open 名称/编号/ID　打开已有案例')
        self.output('/resume　继续上次会话（只读恢复，不自动执行）')
        self.output('/help 帮助　/home 返回首页　/quit 退出')

    def home(self):
        self.save()
        self.catalog, self.ctx = None, None
        self.selection = dict(request_id=None, baseline_version_id=None)
        self.show_home()

    def resume(self):
        case_id=self.state.get('case_id')
        if not case_id and self.legacy_selection:
            legacy=dict(self.legacy_selection)
            rid=legacy.get('request_id')
            ctx=self.runner.data('context',request_id=rid) if rid else None
            catalog=self.runner.data('discover',case_id=(ctx or {}).get('case_id') or (ctx or {}).get('project_id'))
            case_id=catalog.get('case_id') or catalog.get('project_id')
            if case_id:
                self.state['cases'][case_id]=legacy
                self.legacy_selection=None
        if not case_id:
            self.output('尚无可恢复的上次会话；请用 /case import 导入，或 /case list 查看已有案例。')
            return
        self.switch_case(dict(case_id=case_id))

    def select_baseline(self):
        available = [row['version_id'] for row in self.catalog['baselines'] if row.get('selectable', True)]
        chosen = self.selection.get('baseline_version_id')
        if chosen not in available or not self.selection.get('baseline_explicit'):
            self.selection['baseline_version_id'] = (self.catalog.get('default_baseline') or {}).get('version_id')

    def show_case(self):
        self.output('当前案例：' + (self.catalog.get('project_label') or '尚无案例，请先 /case import 目录'))
        self.output('案例ID：' + str(self.catalog.get('case_id')) + '；项目：' + str(self.catalog.get('project_id')))
        completion=((self.ctx or {}).get('verified_source_facts') or {}).get('baseline_sysml_completion') or {}
        if self.catalog.get('case_status'):
            self.output(('历史导入状态：' if completion.get('status')=='VERIFIED' else '案例状态：') + shown(self.catalog['case_status']))
        if completion.get('status')=='VERIFIED':
            self.output('案例SysML状态：原基准可信侧证已补齐并验证；原v0历史文件仍无SysML。')
        self.show_intake(self.catalog)

    def restore_request(self):
        if self.selection.get('request_id'):
            try:
                self.refresh()
                self.show_context()
                if self.ctx.get('plan'):
                    self.show_plan(self.ctx['plan'], review=False)
                self.output('已只读重开，未调用模型或自动恢复执行。')
            except ActionError as exc:
                self.report_error(exc)
                self.output('上次请求未能安全载入；保留原选择，请用 /open ID 显式选择。')
        else:
            self.output('当前没有请求。直接输入中文新需求；先记录原文，再设置并明确批准有限预算。')

    def show_baseline(self):
        latest = self.catalog.get('default_baseline')
        if latest:
            label = '当前采样路线诊断通过的参考' if latest.get('task_status') == 'PASS' else '默认可选参考（历史路线记录不代表当前物理有效）'
            self.output(f"{label}：{latest['version_id']}，总质量 {latest.get('parameters', {}).get('m', '未知')} kg；{self.stair_description(latest.get('parameters'))}；{listed_result(latest)}。")
        for row in self.catalog['baselines']:
            if row['version_id'] == self.selection.get('baseline_version_id'):
                self.output(f"新需求基线：{row['version_id']}，总质量 {row.get('parameters', {}).get('m', '未知')} kg；{self.stair_description(row.get('parameters'))}；{listed_result(row)}。")
                if row.get('source_sha256'):
                    self.output('基线源码SHA256：' + row['source_sha256'])
                capability = row.get('input_capability') or {}
                from .case_asym2_family import FAMILY as ASYM2_FAMILY
                if self.catalog.get('support',{}).get('family')==ASYM2_FAMILY:
                    self.output('输入支持范围：固定Asym2非均匀20级模型；仅独立改变原重心点载总质量或主体楼梯高/深，尾阶、车辆、接触和控制冻结；实际任务结果须由新运行的可信检查判定，真实车辆能力未验证。')
                elif capability.get('message'):
                    self.output('输入支持范围：' + capability['message'])
                else:
                    self.output('输入支持范围：当前旧方程家族仅作继承模型诊断；正有限的楼梯高度/深度不代表能爬过，未知能力须由可信结果判定。车辆和执行范围仍按所选基线核查。')
                return
        self.output('当前没有可选基线；请先 /case import 导入具备必要资料的案例。')

    def mode(self):
        if self.catalog is None:
            return '首页'
        if not self.ctx:
            return '新需求' if not self.selection.get('request_id') else '请求未载入（只读）'
        plan = self.ctx.get('plan') or {}
        actual = self.ctx.get('execution_state') or ((self.ctx.get('stage_facts') or {}).get('current') or {}).get('execution_state')
        if actual == 'UNCERTAIN':
            return '执行状态UNKNOWN（只读核实）'
        if actual == 'RUNNING':
            return '验证进行中（只读）'
        if actual == 'TERMINAL' or self.ctx.get('terminal_saved'):
            return '只读追问'
        if plan.get('support') == 'NEEDS_INPUT' and self.ctx.get('access_mode') != 'RESULT_REVIEW_ONLY':
            return '补充缺项（同账）'
        if (self.ctx.get('last_failure') or {}).get('stopped_action'):
            return '生成停止（查看 /status）'
        if plan.get('intent') == 'CHANGE' and plan.get('support') == 'SUPPORTED' and not plan.get('missing') and not plan.get('unsupported') and self.ctx.get('access_mode') != 'RESULT_REVIEW_ONLY':
            return '方案已确认，待执行' if self.ctx.get('confirmation_path') else '方案待确认执行'
        if plan:
            return '只读追问'
        return '已记录需求（待预算/方案）'

    def show_context(self):
        ctx = self.ctx
        if not ctx:
            self.output('尚未选择请求。')
            return
        actual_stage = {'TERMINAL': '执行已结束', 'RUNNING': '验证进行中', 'UNCERTAIN': '执行状态UNKNOWN'}.get(ctx.get('execution_state'), shown(ctx['stage']))
        self.output(f"当前请求：{ctx['request_id']}；基线 {ctx['baseline_version_id']}；{actual_stage}。")
        self.output('需求：' + ctx['raw_request'])
        if (ctx.get('input_capability') or {}).get('message'):
            self.output('输入支持范围：' + ctx['input_capability']['message'])
        facts = ctx.get('verified_source_facts') or {}
        if facts.get('selected_baseline_sysml_present') is True:
            self.output('已核基准事实：当前请求冻结基线 ' + ctx['baseline_version_id'] + ' 已包含SysML，无需再补原基准。')
        elif (facts.get('baseline_sysml_completion') or {}).get('status')=='VERIFIED':
            self.output('原基准SysML：可信侧证已补齐并验证；原v0历史文件仍无SysML，未覆盖原件，无需重复补齐。')
        elif (facts.get('baseline_sysml_completion') or {}).get('status')=='UNKNOWN':
            self.output('原基准SysML侧证：绑定尚未验证，当前仍UNKNOWN。')
        self.output('当前输入模式：' + self.mode() + '；修改需求请用 /new。')
        from .case_stage_facts import stage_lines
        for line in stage_lines(ctx.get('stage_facts'), request_id=ctx['request_id']):
            self.output(line)
        if self.mode() in ('方案待确认执行', '方案已确认，待执行'):
            self.output('普通文字仍为只读追问；只有完整审阅方案后单独输入“确认执行”才启动生成与执行。')
        self.show_missing_options()
        if ctx.get('proposal_unknown'):
            self.output('提案保存结果UNKNOWN；只读查询，不自动恢复。')
        if ctx.get('budget'):
            self.show_budget(ctx['budget'], compact=True)
        else:
            self.output('尚无预算批准，产品HTTP及执行未获准。')
        self.show_failure()

    def help(self):
        self.output('/home 返回首页；/resume 显式恢复上次案例与会话，只读查询，不自动恢复执行或预算。')
        self.output('进入案例后直接输入：无请求时建立新需求；已有方案/结果时只读追问；缺项时补充事实。')
        self.output('/new [需求] 新需求　/status 状态　/result 简洁结果　/result full 完整说明与详细字段（只读）　/sessions 请求列表　/open ID 只读打开')
        self.output('/ask 问题 显式只读咨询（可能消耗原预算HTTP）　/defer 原因 缺项阶段本地记录资料暂缺，不生成或执行')
        self.output('/recover 追加批准文件　仅接续已证MCP派发前本地导入失败，复用原双源码与原账；真实UNKNOWN不重发')
        self.output('/recover connect 用户补充许可文件　仅对已证连接阶段失败，按原payload进行一次明确许可的方案请求替换；原UNKNOWN及全预留保留')
        self.output('/recover response 完整保存回复零HTTP恢复；/recover plan 展示一次读取超时方案替换建议，精确输入“确认恢复方案”才发送，可能重复计费。')
        self.output('/baseline [版本] 查看/选择新需求基线　/budget 设置或查看有限预算')
        self.output('/plan 完整审阅已保存方案；尚无方案时显式提出方案　/plan brief 只读摘要，不发起提案或替代完整审阅')
        self.output('/case list 案例列表　/case import [目录] 导入　/case open 名字/编号/ID 切换　/case replay 名字/编号/ID 独立全流程测试')
        self.output('/compare v000002 v000003 对比当前案例版本；切换和对比只读，不调用模型或执行。')
        self.output('确认执行：仅在当前完整、受支持方案已展示后生效；/help 帮助　/quit 退出。')
        self.output('预算和方案分别需要主动确认。空回车、否定、句子含“确认”或模型自称批准都不批准。')

    def report_error(self, exc):
        self.output(str(exc))
        if getattr(exc, 'evidence', None):
            self.output('证据：' + exc.evidence)
        if getattr(exc, 'kind', None) and self.selection.get('request_id'):
            try:
                self.refresh()
                self.show_failure()
                if self.ctx and self.ctx.get('budget'):
                    self.show_budget(self.ctx['budget'], compact=True)
            except (ActionError, ValueError, OSError, KeyError):
                self.output('当前状态未能读回，请用 /status 查看已保存证据。')

    def show_failure(self):
        failure = (self.ctx or {}).get('last_failure') or {}
        if failure.get('status') == 'UNRESOLVED':
            if failure.get('stopped_action'):
                self.output(failure['stopped_action'])
            self.output('最后校验原因：' + str(failure.get('error', '未知')))
            if failure.get('revalidation_error'):
                self.output(failure['revalidation_error'])
            self.output('失败证据：' + str(failure.get('evidence_path', '未知')))
        elif failure.get('status') == 'RESOLVED_BY_OFFLINE_REVALIDATION':
            self.output('历史来源协议失败已离线复验；待执行。原失败记录保留。')

    @staticmethod
    def format_value(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)

    @staticmethod
    def stair_description(parameters):
        values = parameters or {}
        return '楼梯高度 ' + str(values.get('stepH', '未知')) + ' m × 深度 ' + str(values.get('stepD', '未知')) + ' m'

    def show_intake(self, value):
        support = value.get('support')
        if support:
            self.output('输入支持：' + shown(support.get('status')) if isinstance(support, dict) else '输入支持：' + shown(support))
            if isinstance(support, dict):
                if support.get('scope'):
                    self.output('支持范围：' + support['scope'])
                for reason in support.get('reasons') or []:
                    self.output('原因：' + intake_reason(reason))
        for item in value.get('missing') or []:
            text = self.format_value(item)
            label = '历史导入缺项（原v0）' if value.get('baselines') and 'sysml' in text.lower() else '缺项'
            self.output(label + '：' + text)
        # A saved import's v0 obligations do not describe the selected request.
        # Show concrete generation work only with an executable selected plan.
        if not value.get('baselines'):
            for item in value.get('generation_obligations') or value.get('intake_obligations') or []:
                self.output('拟补齐及影响：' + self.format_value(item))
        statement = value.get('success_statement') or (value.get('case') or {}).get('historical_success_statement')
        if statement:
            self.output('用户历史成功范围原文：' + statement)
        else:
            self.output('历史标记：用户提供参考，历史成功范围未独立核实。')

    def case_rows(self):
        value = self.runner.projects('list')
        return value['cases']

    def resolve_case(self, token, rows=None):
        token = token.strip()
        if token and (token[0] in ('"', "'") or token[-1] in ('"', "'")):
            if len(token) < 2 or token[0] not in ('"', "'") or token[-1] != token[0]:
                raise ActionError('案例名称的引号未配对；请使用完整名称、编号或唯一ID。')
            token = token[1:-1]
        rows = self.case_rows() if rows is None else rows
        if token.isdecimal() and 1 <= int(token) <= len(rows):
            return rows[int(token) - 1]
        matches = [row for row in rows if token in (row.get('case_id'), row.get('name'), row.get('project_id'))]
        if len(matches) != 1:
            raise ActionError('案例不存在或名字重复；请用 /case list 中的编号或唯一ID。')
        return matches[0]

    def list_cases(self):
        rows = self.case_rows()
        for index, row in enumerate(rows, 1):
            kind = '独立测试' if row.get('kind') in ('independent_test', 'INDEPENDENT_TEST', 'replay', 'independent') or row.get('origin_case_id') else '正式'
            source = row.get('original_input_root') or row.get('source_path') or row.get('source_directory') or row.get('source') or row.get('origin_case_id') or '原有项目'
            current_case=(self.catalog or {}).get('case_id')
            session = self.selection if row['case_id'] == current_case else self.state['cases'].get(row['case_id'], {})
            selected = session.get('baseline_version_id') or row.get('default_baseline_version_id') or '未选择'
            latest = row.get('latest_version_id') or row.get('latest_version') or '未知'
            marker = ' [当前]' if row['case_id'] == current_case else ''
            self.output(f"{index}. {row.get('name', row['case_id'])} | {kind}{marker} | {row['case_id']} | 来源 {self.format_value(source)} | 所选 {selected}；最近 {self.format_value(latest)}")
        if not rows:
            self.output('尚无案例；用 /case import 目录 导入原始资料。')

    def switch_case(self, row, *, imported=False):
        case_id = row['case_id']
        catalog = self.runner.data('discover', case_id=case_id)
        catalog.setdefault('case_id', case_id)
        previous_case=(self.catalog or {}).get('case_id')
        self.save()
        if case_id != previous_case:
            self.selection = dict(self.state['cases'].get(case_id, dict(request_id=None, baseline_version_id=None)))
        self.catalog, self.ctx = catalog, None
        self.selection.pop('displayed_digest', None)
        self.selection.pop('displayed_request_id', None)
        self.select_baseline()
        if imported:
            baseline = self.catalog.get('default_baseline') or {}
            self.output('案例已保存：' + self.catalog['project_label'] + f"；原基准 {baseline.get('parameters', {}).get('m', '未知')} kg（导入未运行）。")
            self.output('历史标记：用户提供参考，历史成功范围未独立核实。')
            if self.catalog.get('generation_obligations') or self.catalog.get('intake_obligations'):
                self.output('缺少或尚未验证的SysML，将在方案确认后由产品补齐并检查。')
            self.save()
            support = self.catalog.get('support') or {}
            if (support.get('status') if isinstance(support, dict) else support) == 'UNSUPPORTED':
                self.output('此模型目前仅可管理资料，不能进入执行。')
            else:
                self.output('这次希望在原案例上做什么改变？')
            return
        self.show_case()
        self.show_baseline()
        self.restore_request()
        self.save()
        self.output('已恢复该案例的需求、基线和会话记忆；未调用模型或恢复执行。')

    def choose_model(self, inspected):
        candidates = inspected.get('model_candidates') or []
        supported = [item for item in candidates if (item.get('support') or {}).get('status') == 'SUPPORTED']
        others = [item for item in candidates if item not in supported]
        files = inspected.get('files') or []
        documents = [item['path'] for item in files if item.get('kind') != 'modelica' and not str(item.get('path', '')).lower().endswith('.mo')]
        self.output(f'原始资料：{len(candidates)} 份Modelica模型，{len(documents)} 份其他资料；全部原样保存。')
        if documents:
            self.output('其他资料：' + '、'.join(documents))
        numbered = supported if supported else candidates
        self.output('可用于当前改型流程的模型：' if supported else '当前没有受支持的执行模型；以下模型仅可导入管理：')
        for index, item in enumerate(numbered, 1):
            self.output(f"  {index}. {item['path']}" + ('（' + item['model_name'] + '）' if item.get('model_name') else ''))
            support = item.get('support') or {}
            if support.get('status') == 'SUPPORTED':
                parameters = support.get('parameters') or {}
                def value(name, scale=1):
                    number = parameters.get(name)
                    return f'{number * scale:g}' if type(number) in (int, float) and math.isfinite(number) else '未知'
                summary = f"总质量{value('m')}kg；主轮直径{value('R_main', 2000)}mm；主体H×D={value('stepH', 1000)}×{value('stepD', 1000)}mm；{value('n_steps')}级"
                if support.get('family') == 'sixwheel_full_mission_asym2_v1':
                    summary += '；非均匀：前18级＋尾阶'
                self.output('     ' + summary)
            else:
                reasons = support.get('reasons') or ['当前未验证支持此模型。']
                self.output('     ' + '；'.join(intake_reason(reason) for reason in reasons))
        if supported:
            for item in others:
                reasons = (item.get('support') or {}).get('reasons') or ['当前未验证支持此模型。']
                self.output('仅作资料：' + item['path'] + '；' + '；'.join(intake_reason(reason) for reason in reasons))
        if not candidates:
            return None
        if len(candidates) == 1:
            self.output('仅有一个模型，采用：' + candidates[0]['path'])
            return candidates[0]['path']
        example = numbered[0]
        counts = f'目录共{len(candidates)}份MO，其中{len(supported)}份支持当前改型'
        choice = self.input(f"{counts}。请明确选择：输入编号（如1）、精确相对路径（如{example['path']}）或唯一模型名；空回车取消：").strip().strip('"')
        selected = None
        if choice.isdecimal() and 1 <= int(choice) <= len(numbered):
            selected = numbered[int(choice) - 1]
        elif choice:
            normalized = choice.replace('\\', '/')
            matches = [item for item in candidates if normalized == item['path'].replace('\\', '/') or choice == item.get('model_name')]
            if len(matches) == 1:
                selected = matches[0]
        if selected is None:
            self.output('未明确选择唯一模型，未创建案例；请按显示的编号、相对路径或唯一模型名选择。')
            return None
        return selected['path']

    def import_case(self, path):
        path = path.strip().strip('"')
        if not path:
            path = self.input('原始案例目录（空回车取消）：').strip().strip('"')
        if not path:
            self.output('已取消，未导入案例。')
            return
        inspected = self.runner.projects('inspect', path=path)
        model = self.choose_model(inspected)
        if model is None:
            self.output('未选择可管理的Modelica文件，未创建案例。')
            return
        inspected = self.runner.projects('inspect', path=path, model=model)
        duplicates = inspected.get('duplicates') or []
        duplicate_mode = None
        if duplicates:
            self.output('发现相同模型或资料。可打开已有项目，或创建不跳过任何业务步骤的独立全流程测试。')
            for index, item in enumerate(duplicates, 1):
                if item.get('match') == 'SAME_MODEL_SOURCE':
                    self.output('匹配依据：模型源码相同；不代表整个资料包相同。')
                elif item.get('match') in ('SAME_INPUT_PACKAGE', 'SAME_INPUT_BUNDLE'):
                    self.output('匹配依据：原始资料包相同。')
                self.output(f"  {index}. {item.get('name', '已有案例')}；{item['case_id']}")
            choice = self.input('输入“打开 编号”或“独立测试”；其他输入（含空回车）取消：').strip()
            if choice.startswith('打开 '):
                target = choice[3:].strip()
                self.switch_case(self.resolve_case(target, duplicates))
                return
            if choice != '独立测试':
                self.output('已取消，未创建重复项目。')
                return
            duplicate_mode = 'independent'
        name = self.input('新案例名称（空回车取消）：').strip()
        if not name:
            self.output('已取消，未创建案例。')
            return
        result = self.runner.projects('import', path=path, name=name, model=model, duplicate_mode=duplicate_mode)
        if not result.get('created', 'case' in result):
            self.show_intake(result.get('inspection') or result)
            self.output('未创建案例；请补齐明确选择或按重复提示操作。')
            return
        self.history('case_imported', case_id=result['case']['case_id'], duplicate_mode=duplicate_mode, product_approval=False)
        self.switch_case(result['case'], imported=True)

    def replay_case(self, token):
        row = self.resolve_case(token)
        model = row.get('selected_model')
        if not model:
            inspected = self.runner.projects('inspect', path=row['original_input_root'])
            model = self.choose_model(inspected)
            if model is None:
                return
        name = self.input('独立全流程测试名称（空回车取消）：').strip()
        if not name:
            self.output('已取消，未创建独立测试。')
            return
        self.output('独立测试从原始资料和新需求开始；旧方案、批准、预算及PASS不复用，后续仍须确认预算和方案。')
        result = self.runner.projects('replay', case_id=row['case_id'], name=name, model=model)
        if not result.get('created', 'case' in result):
            self.show_intake(result.get('inspection') or result)
            self.output('未创建独立测试；缺项或模型选择仍需明确。')
            return
        self.history('case_replayed', case_id=result['case']['case_id'], origin_case_id=row['case_id'], product_approval=False)
        self.switch_case(result['case'], imported=True)

    def compare(self, words):
        versions = words.split()
        if len(versions) != 2:
            self.output('用法：/compare v000002 v000003，比较当前案例的两个已有版本。')
            return
        result = self.runner.projects('compare', case_id=self.catalog['case_id'], left=versions[0], right=versions[1])
        self.output('版本对比（只读）：' + self.catalog['project_label'])
        # Render all backend evidence fields without inferring acceptance or
        # ranking from completion time. Conditions are assessed by the backend.
        for key, value in result.items():
            if key in ('left', 'right') and isinstance(value, dict):
                self.output(('左版' if key == 'left' else '右版') + '：')
                for field, item in value.items():
                    labels = dict(version_id='版本', parameters='参数', baseline_source='基线来源', task_conditions='任务条件', tool_status='工具', chain_status='链路', task_status='任务', physical_acceptance_status='实物', completion_time='完成时间')
                    if field == 'mission_assessment' and isinstance(item, dict):
                        keys = ('task_status', 'route_status', 'diagnostic_status', 'overall_summary', 'body_motion_observations', 'validity_status', 'completion_status', 'completion_time', 'descent_orientation', 'exit_x', 'rear_edge_at_completion', 'post_exit_distinct_times', 'issues', 'limitations')
                        summary = {key: item[key] for key in keys if key in item}
                        summary.update({key + '_count': len(body) for key, body in item.items() if isinstance(body, list) and key not in ('issues', 'limitations')})
                        self.output('独立任务检查摘要：' + self.format_value(summary))
                        continue
                    self.output(labels.get(field, field) + '：' + self.format_value(item))
            else:
                self.output(key + '：' + self.format_value(value))
        if result.get('comparable') is False or result.get('directly_comparable') is False or result.get('conditions_match') is False:
            self.output('证据不完整或基线/任务条件不一致，不可直接排名。')
        self.output('完成时间仅供展示与比较，不自动形成业务验收门槛；UNKNOWN/未运行不当作PASS。')
        self.output('完整对比与事件证据：' + str(self.root / 'operations') + '；源码与数据路径见上方。')

    def open_request(self, rid):
        ctx = self.runner.data('context', request_id=rid)
        if not self.catalog or ctx['project_id'] != self.catalog['project_id']:
            row = self.resolve_case(ctx['project_id'])
            self.output(('将只读打开该请求所属案例：' if not self.catalog else '该请求属于另一案例，将明确只读切换到其所属项目：') + row.get('name', row['case_id']))
            self.switch_case(row)
        self.ctx = ctx
        self.selection['request_id'] = rid
        self.selection.pop('displayed_digest', None)
        self.selection.pop('displayed_request_id', None)
        self.save()
        self.show_context()
        self.output('已只读打开；未调用模型或恢复执行。')

    def budget_guard(self, *, execute=False):
        self.refresh()
        if not self.ctx or not self.ctx.get('budget'):
            self.output('尚无明确批准的有限预算，请用 /budget。')
            return False
        budget = self.ctx['budget']
        if self.ctx['request_id'] in self.selection.get('interrupted_requests', []):
            self.output('本终端曾中断此请求，执行结果需只读核实；禁止自动恢复或重发。')
            return False
        if budget.get('blocked') or self.ctx.get('proposal_unknown'):
            self.output((budget.get('blocked') or '提案结果UNKNOWN') + '；仅保留只读查询，禁止换ID续借。')
            return False
        if execute and self.ctx.get('execution_state') in ('RUNNING', 'UNCERTAIN'):
            self.output('任务仍在运行或结果UNKNOWN；请只读核实，不能重复执行。')
            return False
        if not execute and budget.get('http_blocked'):
            self.output(budget['http_blocked'] + '；停止新增产品HTTP，已有结果和其他动作分别检查。')
            return False
        if execute and (self.ctx.get('last_failure') or {}).get('stopped_action'):
            self.show_failure()
            self.show_budget(budget, compact=True)
            return False
        if execute and not budget['permissions'].get('execution'):
            self.output('当前预算没有执行权限。')
            return False
        return True

    def plan(self, *, brief=False):
        self.refresh()
        if self.ctx and self.ctx.get('plan'):
            self.show_plan(self.ctx['plan'], brief=brief)
            return
        if brief:
            self.output('当前没有已保存方案；请先用 /plan 提出并完整审阅方案。摘要不会调用模型。')
            return
        if not self.budget_guard():
            return
        if self.ctx.get('terminal_saved'):
            self.output('已有终态结果，使用 /result 或只读追问；不自动恢复执行。')
            return
        self.output('阶段：产品理解需求并提出方案，尚不生成源码或执行。')
        result = self.runner.call('plan-request', request_id=self.ctx['request_id'])
        self.refresh()
        self.show_plan(result.get('plan') or self.ctx.get('plan'))

    def show_plan(self, plan, *, review=True, brief=False):
        if not plan:
            self.output('当前尚无产品方案。')
            return
        columns = terminal_columns()
        def emit(text='', *, indent='', continuation='  '):
            for line in wrapped_lines(text, columns, indent=indent, continuation=continuation):
                self.output(line)

        def section(title):
            emit()
            emit(title)

        def value(label, item, *, indent='  '):
            if isinstance(item, dict) and item:
                emit(label + '：', indent=indent)
                for key, child in item.items():
                    value(str(key), child, indent=indent + '  ')
            elif isinstance(item, list) and item:
                emit(label + '：', indent=indent)
                for index, child in enumerate(item, 1):
                    value(str(index), child, indent=indent + '  ')
            else:
                emit(label + '：' + self.format_value(item), indent=indent, continuation=indent + '  ')

        def items(title, rows, *, empty='无'):
            section(title)
            if not rows:
                emit(empty, indent='  ')
            for index, item in enumerate(rows or [], 1):
                value(str(index), item)

        ctx = self.ctx or {}
        executable = plan.get('intent') == 'CHANGE' and plan.get('support') == 'SUPPORTED' and not plan.get('missing') and not plan.get('unsupported') and ctx.get('access_mode') != 'RESULT_REVIEW_ONLY'
        version = ctx.get('baseline_version_id', self.selection.get('baseline_version_id'))
        row = next((item for item in (self.catalog or {}).get('baselines', []) if item['version_id'] == version), {})
        parameters = ctx.get('baseline_parameters') or row.get('parameters') or {}
        proposed = plan.get('proposed_parameters') or {}
        facts = ctx.get('verified_source_facts') or {}
        present = facts.get('baseline_sysml_available',facts.get('selected_baseline_sysml_present'))
        requires_sysml = not present if isinstance(present, bool) else row.get('requires_sysml', ctx.get('baseline_requires_sysml', False))
        emit(('方案摘要' if brief else '方案完整审查') + '：' + shown(plan.get('support')) + '；' + ('只读问题' if plan.get('intent') == 'QUESTION' else '变更需求'))
        emit('请求：' + str(ctx.get('request_id') or '未知'))
        emit('方案绑定基线：' + str(version or '未知') + '（当前请求冻结基线）')
        if ctx.get('terminal_saved'):
            emit('以下为当时保存的方案；当前请求已有结果，实际运行结论请用 /result。')
        if brief:
            emit('只读参数与风险摘要；完整设计、依据、假设和默认请用 /plan 审阅。')

        changed = [key for key in proposed if key not in parameters or proposed[key] != parameters[key]]
        has_target = executable and bool(proposed)
        section('一、本次变更参数' if has_target else '一、方案列明的目标参数（尚无可执行目标）')
        if not changed:
            emit('无已列明的参数变更。', indent='  ')
        for key in changed:
            emit(parameter_label(key) + '：' + parameter_value(key, parameters.get(key)) + ' → ' + parameter_value(key, proposed[key]), indent='  ')
        section('二、保持不变的参数（继承冻结基线）' if has_target else '二、基线参考值（尚无可执行目标）')
        inherited = [key for key in parameters if key not in changed]
        if not inherited:
            emit('未提供可核对的继承参数；其余继承范围见方案原文。', indent='  ')
        for key in inherited:
            emit(parameter_label(key) + '：' + parameter_value(key, parameters[key]) + ('（保持不变）' if has_target else ''), indent='  ')
        value('结构化允许差异范围', plan.get('scope') or {})

        design = plan.get('design') or {}
        if not brief:
            section('三、需求与来源原文')
            value('用户需求原文', ctx.get('raw_request', '未提供'))
            for index, requirement in enumerate(plan.get('requirements') or [], 1):
                emit(str(index) + '. 需求', indent='  ')
                if isinstance(requirement, dict):
                    value('需求', requirement.get('text', ''), indent='    ')
                    value('来源原文', requirement.get('source_text', ''), indent='    ')
                else:
                    value('需求', requirement, indent='    ')
            section('四、设计与依据（原文）')
            value('设计', design.get('summary', '未提供'))
            for index, parameter in enumerate(design.get('parameters') or [], 1):
                emit(str(index) + '. ' + parameter_label(parameter.get('name', '未知')) + '：' + self.format_value(parameter.get('value')) + (' ' + str(parameter['unit']) if parameter.get('unit') else ''), indent='  ')
                value('依据', parameter.get('basis', ''), indent='    ')
            value('理由', plan.get('rationale', '未提供'))
            items('五、全部假设（逐项原文）', [item.get('text', '') if isinstance(item, dict) else item
                                               for item in design.get('assumptions') or []])
            items('六、全部默认（逐项原文）', plan.get('defaults'))
        items('需留意：未知与未验证', plan.get('unknowns'))
        items('必须补充', plan.get('missing'))
        items('不支持的范围', plan.get('unsupported'))

        section('SysML补齐与已核事实')
        if executable and requires_sysml:
            emit('拟补齐：确认后由产品生成原基准SysML侧证与改型SysML/Modelica，并检查来源映射和一致性；原v0保持导入未运行。缺关键关系时应澄清，不由开发代理猜补。')
            obligations = (self.catalog or {}).get('generation_obligations') or (self.catalog or {}).get('intake_obligations') or []
            for item in obligations:
                value('拟补齐及影响', item)
        if present is True:
            emit('原基准SysML：可信侧证已补齐并验证；原v0历史文件仍无SysML，无需重复补齐。' if (facts.get('baseline_sysml_completion') or {}).get('status')=='VERIFIED' and facts.get('selected_baseline_sysml_present') is not True else '已核基准事实：当前冻结基线 ' + str(version) + ' 已包含SysML，无需再补原基准。')
            if executable:
                emit('本请求工作：确认后仅同步生成候选SysML与Modelica双源码，并检查来源映射和一致性。')
        elif not requires_sysml:
            emit('当前补齐规则未要求重补原基准SysML；未提供独立已核事实时不宣称已核。')
        if plan == ctx.get('plan'):
            for warning in ctx.get('plan_review_warnings') or []:
                emit('已核事实更正：' + str(warning))
        if brief:
            section('下一步')
            emit('完整审阅请用 /plan；本摘要不新增完整审阅标记，不替代确认。')
            return

        section('技术追溯（原始字段）')
        if row.get('source_sha256'):
            value('基线源码SHA256', row['source_sha256'])
        value('scope', plan.get('scope') or {})
        section('下一步')
        if executable and review:
            self.selection['displayed_digest'] = self.ctx['displayed_digest']
            self.selection['displayed_request_id'] = self.ctx['request_id']
            self.save()
            budget = self.ctx.get('budget')
            if budget and not budget.get('blocked') and budget['permissions'].get('execution') and not self.ctx.get('terminal_saved'):
                emit('请完整审阅以上方案。明确同意后单独输入“确认执行”；在此之前不会生成或运行。')
            elif self.ctx.get('terminal_saved'):
                emit('以上是历史已保存方案；本请求已有终态，不自动恢复或重跑。')
            else:
                emit('当前预算未准许新增执行，只可审阅方案和查询已有结果。')
        elif not executable:
            self.selection.pop('displayed_digest', None)
            self.selection.pop('displayed_request_id', None)
            self.save()
            emit('没有可执行方案（NO_EXECUTABLE_PLAN）。' + ('可直接补充事实；保持同一预算及原截止。' if self.missing_input() else '可只读追问；改变目标请用 /new。'))
            self.show_missing_options(emit)
        else:
            self.selection.pop('displayed_digest', None)
            self.selection.pop('displayed_request_id', None)
            emit('以上为已保存方案；需要执行时先 /plan 展示并审阅当前方案。')

    def new(self, text):
        if not self.catalog.get('case_id'):
            self.output('请先 /case import 导入原始案例，或 /case open 选择案例。')
            return
        if not text.strip():
            text = self.input('新需求原文（空回车取消）：').strip()
        if not text:
            self.output('已取消，未创建请求。')
            return
        self.catalog = self.runner.data('discover', case_id=self.catalog['case_id'])
        support = self.catalog.get('support')
        if (support.get('status') if isinstance(support, dict) else support) == 'UNSUPPORTED':
            self.show_intake(self.catalog)
            self.output('当前案例超出执行支持范围；不能创建可执行需求或承诺运行。')
            return
        if not self.selection.get('baseline_explicit'):
            self.selection['baseline_version_id'] = (self.catalog.get('default_baseline') or {}).get('version_id')
        if not self.selection.get('baseline_version_id'):
            self.output('没有可选基线；导入参考无须先有新执行PASS，必要输入仍须齐全。')
            return
        # A new, distinct task is allowed; an existing UNKNOWN task cannot be
        # relabeled as a retry. This explicit distinction is shown beforehand.
        if self.ctx and (self.ctx.get('budget') or {}).get('blocked'):
            self.output('当前请求已停止；/new 只能建立独立新目标，不能给原请求重试或续借。')
            if text.strip() == self.ctx['raw_request'].strip():
                self.output('相同原文不换ID重试，请保留原请求并只读查询。')
                return
        rid = 'console_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:10]
        self.runner.call('request', request_id=rid, project_id=self.catalog['project_id'], baseline_version=self.selection['baseline_version_id'], requirement=text)
        self.selection.update(request_id=rid)
        self.selection.pop('displayed_digest', None)
        self.selection.pop('displayed_request_id', None)
        self.save()
        self.history('new_request', request_id=rid, raw_request=text)
        self.refresh()
        self.show_context()
        self.budget()

    def number(self, label, default, *, integer=True, maximum=None):
        while True:
            value = self.input(f'{label}［建议 {default}，空回车采用建议，/cancel取消］：').strip()
            if value in ('/cancel', '取消'):
                raise ActionError('已取消预算设置，未创建批准。')
            try:
                result = int(value) if value and integer else float(value) if value else default
                if not math.isfinite(result) or result < 0 or maximum is not None and result > maximum:
                    raise ValueError
                return result
            except ValueError:
                self.output('请输入有限非负' + ('整数' if integer else '数值') + (f'，最大 {maximum}' if maximum is not None else '') + '。')

    def budget(self):
        self.refresh()
        if not self.ctx:
            self.output('先输入新需求，再设置本请求预算。')
            return
        if self.ctx.get('budget'):
            self.show_budget(self.ctx['budget'])
            self.show_failure()
            self.output('原预算与截止固定，/budget 不刷新、不追加额度。')
            return
        self.output('有限预算设置：以下只是建议，最后必须单独输入“确认预算”才创建本请求授权。')
        hours, http, money, compile_cap, simulation, candidates, repairs, revisions, extensions = 2, 6, 35, 2, 2, 1, 1, 0, 0
        while True:
            if hours <= 0 or http <= 0 or money <= 0:
                self.output('硬墙钟、HTTP与金额必须大于0；未创建批准。')
                return
            caps = dict(http_requests=http, product_cny=money, total_tokens=http * 1000000, total_output_tokens=http * 393216, compile=compile_cap, simulation=simulation, candidates=candidates, software_repair_rounds=repairs, parameter_revision_rounds=revisions, observation_extensions=extensions)
            try:
                deadline = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            except OverflowError:
                self.output('墙钟上限超出可表示的日期，未创建预算批准。')
                return
            proposed = dict(budget_id='console_budget_' + uuid.uuid4().hex[:20], hard_deadline_utc=deadline, caps=caps, used={key: 0 for key in caps}, permissions=dict(planning_http=True, execution=compile_cap > 0 and simulation > 0 and candidates > 0))
            self.show_budget(proposed)
            if self.execution_timeouts is not None:
                self.output('本请求进程超时（秒，完整字段由既有授权校验器验证）：' + json.dumps(self.execution_timeouts, ensure_ascii=False, sort_keys=True))
            self.output('模型保持 flash/enabled/high，1,000,000共享窗口，max_tokens=393,216；次数、费用、tokens、进程、修复和硬截止全部累计。')
            self.output('方案还须另行确认。未知/在途、到限或过期即停止，不自动换ID或续借。')
            self.output('全部阶段共享上述累计上限；缺项补充事实也沿用同一账本和原硬截止，不刷新额度。')
            approval = self.input('输入“确认预算”批准以上完整建议，或“修改预算”自定义；其他输入（含空回车）均不批准：').strip()
            if approval != '修改预算':
                break
            hours = self.number('从现在起的硬墙钟小时数', hours, integer=False)
            http = self.number('产品HTTP总次数（设计/生成/解释/追问共享）', http)
            money = self.number('人民币总金额上限（元）', money, integer=False)
            compile_cap = self.number('实际编译次数上限', compile_cap)
            simulation = self.number('实际仿真次数上限', simulation, maximum=8)
            candidates = self.number('候选次数上限', candidates)
            repairs = self.number('软件修复轮数上限', repairs)
            revisions = self.number('参数修订轮数上限', revisions)
            extensions = self.number('观察延长次数上限', extensions)
        if approval != '确认预算':
            self.history('budget_declined', request_id=self.ctx['request_id'], entered_text=approval)
            self.output('预算未批准；产品HTTP、生成和执行均未启动。可稍后 /budget。')
            return
        self.history('budget_approved', request_id=self.ctx['request_id'], actual_console_approval=approval, displayed_budget=proposed, cli_approval_word='确认')
        self.runner.call('authorize-request', request_id=self.ctx['request_id'], budget_id=proposed['budget_id'], caps=caps, hard_deadline_utc=deadline, approval_text='确认', planning_only=not proposed['permissions']['execution'], timeouts_s=self.execution_timeouts)
        self.refresh()
        self.plan()

    def show_budget(self, budget, *, compact=False):
        self.output('预算：' + budget['budget_id'] + '；硬总截止 UTC ' + budget['hard_deadline_utc'])
        if budget.get('retained_unknown_after_explicit_connection_replacement'):
            self.output('原连接失败UNKNOWN及完整tokens/费用预留仍保留，实际费用UNKNOWN；唯一同payload替换已知成功，当前动作按原预算接续。')
        if budget.get('retained_unknown_after_explicit_read_timeout_replacement'):
            self.output('原读取超时UNKNOWN、冻结标记及完整预留仍保留，实际费用UNKNOWN；已批准的唯一同payload替换成功，当前请求按原余量和原截止继续。')
        if compact:
            used, caps = budget.get('used', {}), budget['caps']
            items = [('http_requests', 'HTTP'), ('product_cny', '费用CNY'), ('compile', '编译'), ('simulation', '仿真'), ('software_repair_rounds', '自动纠错')]
            self.output('；'.join(f"{label} {used.get(key, 0)}/{caps[key]}" for key, label in items if key in caps))
            local = datetime.fromisoformat(budget['hard_deadline_utc'].replace('Z', '+00:00')).astimezone()
            self.output('本地硬截止：' + local.strftime('%Y-%m-%d %H:%M:%S %z'))
            if budget.get('blocked'):
                self.output(budget['blocked'])
            if budget.get('http_blocked'):
                self.output(budget['http_blocked'] + '（仅限制新增产品HTTP）')
            return
        for key, label in CAP_LABELS.items():
            if key in budget['caps']:
                self.output(f"  {'自动纠错轮数' if key == 'software_repair_rounds' else label}：累计已占 {budget.get('used', {}).get(key, 0):,} / 上限 {budget['caps'][key]:,}")
        if budget.get('blocked'):
            self.output(budget['blocked'])
        if budget.get('http_blocked'):
            self.output(budget['http_blocked'] + '（仅限制新增产品HTTP）')

    def supplement(self, text):
        if not self.budget_guard():
            return
        parent = self.ctx.copy()
        rid = 'console_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:10]
        raw = parent['raw_request'] + '\n\n补充事实原文：\n' + text
        self.output('将原文及本次补充保存为新不可变请求；沿用同一预算、同一累计和原硬截止。')
        self.runner.call('request', request_id=rid, project_id=parent['project_id'], baseline_version=parent['baseline_version_id'], requirement=raw)
        self.history('supplement_parent', request_id=rid, parent_request_id=parent['request_id'], parent_raw_request=parent['raw_request'], supplement_raw_text=text, budget_id=parent['budget']['budget_id'], original_deadline=parent['budget']['hard_deadline_utc'])
        self.runner.call('authorize-request', request_id=rid, budget_id=parent['budget']['budget_id'], reuse_budget=True, approval_text=parent['budget']['approval_quote'])
        self.selection.update(request_id=rid)
        self.selection.pop('displayed_digest', None)
        self.selection.pop('displayed_request_id', None)
        self.save()
        self.refresh()
        self.plan()

    def confirm_execute(self):
        if not self.budget_guard(execute=True):
            return
        if self.ctx.get('terminal_saved'):
            self.output('已有终态结果，不自动恢复或重跑。请用 /result。')
            return
        plan = self.ctx.get('plan') or {}
        if plan.get('intent') != 'CHANGE' or plan.get('support') != 'SUPPORTED' or plan.get('missing') or plan.get('unsupported'):
            self.output('没有完整受支持的可执行方案，不能确认或运行。')
            return
        if self.selection.get('displayed_request_id') != self.ctx['request_id'] or self.selection.get('displayed_digest') != self.ctx['displayed_digest']:
            self.output('请先用 /plan 完整展示当前方案并审阅，再单独输入“确认执行”。')
            return
        delegated = self.delegated_approval_basis is not None
        self.history('explicit_execution_approval', request_id=self.ctx['request_id'], displayed_digest=self.ctx['displayed_digest'], actual_console_approval='确认执行', approval_basis=self.delegated_approval_basis, human_reviewed_displayed_plan=not delegated)
        self.runner.call('confirm-request', request_id=self.ctx['request_id'], displayed_digest=self.ctx['displayed_digest'], approval_text='确认', approval_basis=self.delegated_approval_basis, delegated_confirmation=delegated)
        self.output('方案确认已保存，开始当前动作；实际进展以下方阶段回执为准。')
        result = self.runner.call('run-request', request_id=self.ctx['request_id'])
        self.refresh()
        self.show_result(result)
        self.output('本次动作结束，仍可只读追问；新需求请用 /new。')

    def recover_local(self,approval_path):
        self.refresh()
        if not self.ctx or not approval_path:
            self.output('用法：/recover 已存在的追加批准文件；不创建预算或替代UNKNOWN证明。')
            return
        self.history('explicit_local_predispatch_recovery',request_id=self.ctx['request_id'],approval_evidence=approval_path,actor='AGENT_OR_TRUSTED_LOCAL_OPERATOR',human_reviewed_displayed_plan=False)
        self.runner.call('recover-local-proposal',request_id=self.ctx['request_id'],approval_evidence=approval_path)
        result=self.runner.call('run-request',request_id=self.ctx['request_id'])
        self.refresh();self.show_result(result)

    def recover_connect(self,approval_path):
        self.refresh()
        if not self.ctx or not approval_path:
            self.output('用法：/recover connect 已存在的用户补充许可文件；仅恢复已证连接阶段失败的原方案调用，不扩大预算或确认方案。')
            return
        self.history('explicit_plan_connection_recovery',request_id=self.ctx['request_id'],approval_evidence=approval_path,
                     actor='AGENT_OR_TRUSTED_LOCAL_OPERATOR',human_reviewed_displayed_plan=False)
        result=self.runner.call('recover-plan-connect',request_id=self.ctx['request_id'],approval_evidence=approval_path)
        self.refresh()
        self.show_plan(result.get('plan') or self.ctx.get('plan') or {})

    def recover_response(self):
        self.refresh()
        if not self.ctx:self.output('请先打开原请求。');return
        result=self.runner.call('request-recover-response',request_id=self.ctx['request_id'])
        self.refresh()
        if result.get('plan'):
            self.show_plan(result['plan']);self.output('完整保存方案已零HTTP恢复；方案仍需审阅确认，未生成或仿真。')
        else:
            self.output('完整保存的只读回复已零HTTP恢复：'+str(result.get('recovered_stage','')))
            for line in result.get('program_fact_lines',[]):self.output(line)
            if result.get('supplemental_analysis'):self.output(result['supplemental_analysis'])

    def recover_plan(self):
        self.refresh()
        if not self.ctx:self.output('请先打开原请求。');return
        info=self.runner.call('request-plan-recovery-info',request_id=self.ctx['request_id'])
        template=info['approval_template'];self.pending_plan_recovery=template
        self.output('恢复原请求：'+template['request_id']+'；基线：'+str(template['baseline_ref']))
        self.output('原读取超时，无完整回复；远端是否收到和原费用UNKNOWN。本次同payload替换可能重复计费，原UNKNOWN及完整预留保留。')
        self.output('最多新增1次方案HTTP，最大新增预留5.24288 CNY；原账已占HTTP '+str(info['used'][0])+'/'+str(template['caps']['http_requests'])+'，费用预留/已知 '+str(info['used'][3])+'/'+str(template['caps']['product_cny'])+' CNY。')
        self.output('读取等待600秒、无实质进展600秒；原总截止 UTC '+template['hard_deadline_utc']+'。不会生成、编译或仿真。')
        self.output('明确同意以上一次替换时，单独输入：确认恢复方案。')

    def confirm_plan_recovery(self):
        self.refresh();pending=getattr(self,'pending_plan_recovery',None)
        if not self.ctx or not pending or pending['request_id']!=self.ctx['request_id']:
            self.output('请先用 /recover plan 查看本请求的具体恢复建议。');return
        current=self.runner.call('request-plan-recovery-info',request_id=self.ctx['request_id'])['approval_template']
        if current!=pending:self.pending_plan_recovery=None;raise ActionError('恢复绑定已变化，请重新查看 /recover plan。')
        approval=dict(current,status='APPROVED',approval_channel='TRUSTED_LOCAL_CLI',approval_quote='确认恢复方案',approved_at_utc=datetime.now(timezone.utc).isoformat())
        if self.delegated_approval_basis:approval['delegated_approval_basis']=self.delegated_approval_basis
        path=ROOT/'results/console-plan-recovery-approvals'/(self.ctx['request_id']+'-'+uuid.uuid4().hex+'.json')
        write(path,approval,immutable=True);self.pending_plan_recovery=None
        result=self.runner.call('request-plan-recover-timeout',request_id=self.ctx['request_id'],approval_evidence=str(path))
        self.refresh();self.show_plan(result.get('plan') or self.ctx.get('plan') or {})
        self.output('一次替换已结束。请审阅并确认方案；当前未生成、编译或仿真。')

    def show_result(self, result, *, full=False):
        result, execution, reviewed, headlines = prepare_result(result, request_id=(self.ctx or {}).get('request_id'), stage_context=(self.ctx or {}).get('stage_facts'))
        if not full:
            for line in concise_result_lines(result,execution,reviewed,headlines,evidence_root=(self.ctx or {}).get('root')):
                self.output(line)
            return
        for line in headlines:
            self.output(line)
        if result.get('reused'):
            self.output('以下为已保存结果的只读读回。')
        if result.get('stage'):
            self.output('结果状态：' + shown(result['stage']))
        if reviewed:
            self.output('以下为本只读请求绑定的已有运行结果，不是新运行。')
        if execution:
            self.output('执行版本：' + str(execution.get('version_id', '未知')) + '；任务：' + str(execution.get('task_id', '未知')))
            parameters = execution.get('parameters') or (execution.get('runtime_parameter_evidence') or {}).get('parameters') or {}
            if not parameters:
                spec = execution.get('frozen_spec') or {}
                source = (spec.get('artifact') or {}).get('modelica_source')
                if source:
                    try:
                        from .case_scope import modelica_parameters
                        parameters = modelica_parameters(source)[0]
                    except (ValueError, KeyError, TypeError):
                        pass
            self.output('执行绑定工况：' + self.stair_description(parameters))
            reference = execution.get('reference') or {}
            if execution.get('source_sha256') or reference.get('source_sha256'):
                self.output('执行源码SHA256：' + str(execution.get('source_sha256') or reference['source_sha256']))
            for key, label in (('tool_status', '工具'), ('chain_status', '链路'), ('task_status', '任务'), ('route_status', '路线覆盖'), ('diagnostic_status', '接触诊断'), ('validity_status', '有限采样检查'), ('physical_acceptance_status', '实物')):
                self.output(label + '：' + shown(execution.get(key, 'UNKNOWN')))
            review=execution.get('current_contact_review') or {}
            if review:
                self.output('当前只读接触复核：'+str(review.get('status','UNKNOWN'))+'；有限采样检查 '+str(review.get('validity_status','UNKNOWN'))+'；物理能力 UNKNOWN。')
                self.output('历史记录任务：'+str(execution.get('recorded_task_status','UNKNOWN'))+'；原状态和文件保留，当前结论以本次复核为准。')
                for issue in review.get('issues',[])[:5]:self.output('复核依据：'+str(issue))
                contact=review.get('contact_validity') or {}
                for row in contact.get('hard_contradictions',[])[:3]:
                    boundary=row.get('nearest_boundary') or {}
                    entity={'n':'鼻轮','f':'前主轮','r':'后主轮'}.get(row.get('entity'),'车轮')
                    phase={'up':'上楼','down':'下楼'}.get(boundary.get('phase'),'')
                    step='第'+str(boundary['step'])+'级附近' if boundary.get('step') is not None else ''
                    try:
                        self.output(f"几何证据：{row['time_s']:.6g} s，{entity}整圆处于{phase}{step}楼梯实体内部；x={row['center_x_m']*1000:.1f} mm、z={row['center_z_m']*1000:.1f} mm；距最近边界 {row['center_to_boundary_distance_m']*1000:.3f} mm；d-R={row['full_circle_interior_margin_m']*1000:.3f} mm（采样几何，非求解误差认证）。")
                    except (KeyError,TypeError,ValueError):
                        self.output('几何证据：'+entity+'整圆入楼梯实体的采样异常，完整margin及坐标见绑定复核证据。')
            mission = execution.get('mission') or execution.get('mission_assessment') or {}
            if mission.get('completion_time') is not None:
                self.output(('历史方程路线达成时刻：' if execution.get('task_status')!='PASS' else '路线达成时刻：') + str(mission['completion_time']) + ' s（时间展示，不是物理验收）')
            motion=mission.get('body_motion_observations') or {}
            if motion:
                self.output('车体抬升/回落与姿态观测（无新增验收阈值）：'+self.format_value(motion))
            for key in ('source_files', 'data_files', 'curve_files'):
                files=execution.get(key) or []
                for row in files.values() if isinstance(files,dict) else files:
                    self.output('文件：' + (row.get('path', str(row)) if isinstance(row, dict) else str(row)))
        explanation = result.get('explanation') or {}
        if explanation.get('supplemental_analysis') and explanation.get('explanation_validation'):
            self.output(explanation['supplemental_analysis'])
        elif explanation.get('answer'):
            self.output('历史解释尚未核对当前事实，默认不展示正文；原文保留在本请求证据中。')
        elif explanation.get('status'):
            self.output('产品解释：' + shown(explanation['status']) + '；已有运行结果与解释分别记录。')
        if result.get('proposal') and not execution:
            self.show_plan(result['proposal'].get('plan'))
        if self.ctx:
            self.output('本请求证据：' + self.ctx['root'])

    def show_ask_failure_result(self, request_id):
        """Read the same request again after a refused/failed question, no resend."""
        self.output('本次追问回复未取得；/result 仍可只读查看已有证据，不自动重发。')
        try:
            if self.selection.get('request_id')!=request_id or (self.ctx or {}).get('request_id')!=request_id:return False
            value=self.runner.call('request-result',request_id=request_id,concise=True)
            view,execution,reviewed,lines=prepare_result(value,request_id=request_id)
            state=view.get('state') or {}
            # A current request terminal is required. Historical reviews and
            # stage/plan/display fields never substitute for an execution.
            if (self.selection.get('request_id')!=request_id or (self.ctx or {}).get('request_id')!=request_id
                    or state.get('request_id')!=request_id or not state.get('latest_request_execution')
                    or reviewed or view.get('result_binding_conflict')
                    or execution.get('state') not in ('succeeded','failed','cancelled')
                    or execution.get('current_review_state')!='CURRENT_BOUND_REVIEW'
                    or (execution.get('current_contact_review') or {}).get('execution_evidence_status')!='PASS'
                    or ((self.catalog or {}).get('project_id') and self.catalog['project_id']!=execution.get('project_id'))):return False
            self.output('本次追问回复未取得；已有仿真结果不受影响。以下为本请求当前只读读回：')
            for line in concise_result_lines(view,execution,reviewed,lines,evidence_root=(self.ctx or {}).get('root')):self.output(line)
            return True
        except (ActionError,ValueError,OSError,KeyError,TypeError):
            self.output('已有结果尚未完成当前绑定读回；请用 /result 只读核实，不自动重试追问。')
            return False

    def ask(self, text):
        if not text.strip():
            self.output('用法：/ask 问题；只读产品咨询可能消耗原预算HTTP。')
            return
        if not self.selection.get('request_id'):
            self.output('请先选择请求，或用 /new 记录需求；未调用产品模型。')
            return
        self.output('当前输入为只读追问；改变需求请用 /new。')
        self.output('只读产品咨询可能消耗原预算HTTP，仍受原预算与截止约束。')
        request_id=(self.ctx or {}).get('request_id') or self.selection['request_id']
        if not self.budget_guard():
            self.show_ask_failure_result(request_id)
            return
        self.output('阶段：只读产品问答，不生成或执行。')
        try:
            result = self.runner.call('request-ask', request_id=self.ctx['request_id'], question=text)
        except ActionError as exc:
            if not isinstance(exc,ActionInterrupted):self.report_error(exc)
            self.show_ask_failure_result(request_id)
            if isinstance(exc,ActionInterrupted):raise
            return
        if result.get('status') in ('UNKNOWN','FAILED','NOT_COMPLETED'):
            self.show_ask_failure_result(request_id)
            return
        _,_,_,headlines=prepare_result(dict(state=result.get('binding') or {}),request_id=self.ctx['request_id'])
        self.output(headlines[0])
        if len(headlines)>1 and headlines[1].startswith('能力限制：'):self.output(headlines[1])
        self.output('产品回答（只读，已知事实核对不代表完整语义验证）：')
        if result.get('explanation_validation'):
            from .case_semantic_guard import protect_explanation
            current=protect_explanation(result.get('raw_answer',result.get('answer') or ''),result.get('current_fact_binding') or result.get('binding') or {},source_references=result.get('current_source_references') or ())
            for line in current['program_fact_lines'][1:]:self.output(line)
            self.output(current['supplemental_analysis'])
        else:
            self.output('产品回答尚未核对当前事实，默认不展示正文；原文保留在本请求证据中。')
        self.refresh()

    def handle(self, text):
        text = text.strip()
        if not text:
            return
        if text in ('/quit', '/exit', 'exit', 'quit', '退出'):
            self.quit = True
        elif text in ('/help', '帮助'):
            self.help()
        elif text == '/home':
            self.home()
        elif text == '/resume':
            self.resume()
        elif self.catalog is None and not (text in ('/case','/case list','/case import') or text.startswith(('/case import ','/case open ','/case replay ','/open '))):
            self.output(('当前在首页，未选择案例。' if text=='/status' else '请先选择案例。')+'用 /case import 导入、/case open 打开已有案例，或 /resume 恢复上次会话。')
        elif text == '/case list' or text == '/case':
            self.list_cases()
        elif text == '/case import' or text.startswith('/case import '):
            self.import_case(text[len('/case import'):].strip())
        elif text.startswith('/case open '):
            self.switch_case(self.resolve_case(text[len('/case open '):].strip()))
        elif text.startswith('/case replay '):
            self.replay_case(text[len('/case replay '):].strip())
        elif text == '/compare' or text.startswith('/compare '):
            self.compare(text[len('/compare'):].strip())
        elif text == '/status':
            self.refresh()
            state = self.runner.call('request-status', request_id=self.ctx['request_id']) if self.ctx else None
            if state:
                latest=state.get('latest_request_execution') or state.get('reviewed_execution')
                if latest and latest.get('verification_status')=='NOT_RECHECKED':
                    self.output('【已记录结果】'+latest['version_id']+'；'+listed_result(latest)+'。可信结果请用 /result 绑定内容核实。')
                else:
                    _,_,_,headlines=prepare_result(dict(state=state),request_id=self.ctx['request_id'],stage_context=self.ctx.get('stage_facts'))
                    for line in headlines[:2]:self.output(line)
            self.show_case()
            self.show_baseline()
            self.show_context()
            if self.ctx:
                latest = state.get('latest_request_execution') or state.get('reviewed_execution')
                label = '本请求绑定的已有结果：' if state.get('reviewed_execution') and not state.get('latest_request_execution') else '本请求最近执行：'
                self.output(label + ('尚未运行' if not latest else latest['version_id']+'；'+listed_result(latest) if latest.get('verification_status')=='NOT_RECHECKED' else f"{latest['version_id']}；工具 {shown(latest['tool_status'])}；链路 {shown(latest['chain_status'])}；任务 {shown(latest['task_status'])}；有限采样检查 {shown(latest.get('validity_status','UNKNOWN'))}"))
                if self.ctx.get('plan'):
                    self.show_plan(self.ctx['plan'])
        elif text == '/result' or text.startswith('/result '):
            if text not in ('/result','/result full'):
                self.output('用法：/result 查看简洁结论；/result full 查看完整说明与详细字段。均只读，不调用产品模型或执行。')
                return
            self.refresh()
            if self.ctx:
                self.show_result(self.runner.call('request-result', request_id=self.ctx['request_id'],concise=text!='/result full'),full=text=='/result full')
            else:
                self.output('尚未选择请求。')
        elif text == '/sessions':
            self.catalog = self.runner.data('discover', case_id=self.catalog['case_id'])
            for row in self.catalog['requests']:
                self.output(f"{row['request_id']} | {row['baseline_version_id']} | {shown(row['stage'])} | {row['raw_request'].splitlines()[0]}")
            if not self.catalog['requests']:
                self.output('尚无请求。')
        elif text.startswith('/open '):
            self.open_request(text[6:].strip())
        elif text == '/baseline' or text.startswith('/baseline '):
            self.catalog = self.runner.data('discover', case_id=self.catalog['case_id'])
            chosen = text.partition(' ')[2].strip()
            if chosen:
                if chosen not in [row['version_id'] for row in self.catalog['baselines'] if row.get('selectable', True)]:
                    self.output('只能选择当前案例标记为可选的版本；导入参考仍是未重新运行，不等于PASS。')
                    return
                self.selection['baseline_version_id'] = chosen
                self.selection['baseline_explicit'] = True
                self.save()
            else:
                for row in self.catalog['baselines']:
                    self.output(f"{row['version_id']}：{row.get('parameters', {}).get('m', '未知')} kg；{listed_result(row)}；{'可选' if row.get('selectable', True) else '不可选'}")
                    review=row.get('current_contact_review') or {}
                    if review.get('review_state')=='NOT_REVIEWED':self.output('  当前复核：NOT_REVIEWED；历史记录任务：'+str(row.get('recorded_task_status','UNKNOWN'))+'。')
            self.show_baseline()
            self.output('基线选择只用于下一个新需求；已有请求绑定固定。')
        elif text == '/budget':
            self.budget()
        elif text == '/plan':
            self.plan()
        elif text == '/plan brief':
            self.plan(brief=True)
        elif text == '确认执行':
            self.confirm_execute()
        elif text == '/recover response':
            self.recover_response()
        elif text == '/recover plan':
            self.recover_plan()
        elif text == '确认恢复方案':
            self.confirm_plan_recovery()
        elif text == '/recover connect' or text.startswith('/recover connect '):
            self.recover_connect(text[len('/recover connect'):].strip())
        elif text == '/recover' or text.startswith('/recover '):
            self.recover_local(text[len('/recover'):].strip())
        elif text == '/ask' or text.startswith('/ask '):
            self.ask(text[5:] if text.startswith('/ask ') else '')
        elif text == '/defer' or text.startswith('/defer '):
            self.defer(text[7:] if text.startswith('/defer ') else '')
        elif text == '/new' or text.startswith('/new ') or text.startswith('新需求：'):
            self.new(text[5:] if text.startswith('/new ') else '' if text == '/new' else text[4:])
        elif text.startswith('/'):
            self.output('未知命令；输入 /help 查看。')
        elif self.missing_input() and text.rstrip('。！') in NO_DATA_STATEMENTS:
            self.defer(text)
        elif self.ctx and (self.ctx.get('plan') or {}).get('support') == 'NEEDS_INPUT' and self.ctx.get('access_mode') != 'RESULT_REVIEW_ONLY' and not self.ctx.get('terminal_saved'):
            self.supplement(text)
        elif self.ctx and (self.ctx.get('plan') or self.ctx.get('terminal_saved')):
            self.ask(text)
        elif self.selection.get('request_id'):
            self.output('已有未完成需求。用 /budget 设置预算，/plan 明确提出方案，或 /new 建立独立新需求。')
        else:
            self.new(text)

    def loop(self):
        try:
            if self.catalog is None:
                self.start()
            while not self.quit:
                try:
                    text = self.input('\nv12[' + self.mode() + ']> ')
                    self.handle(text)
                except ActionInterrupted as exc:
                    if self.selection.get('request_id'):
                        ids = self.selection.setdefault('interrupted_requests', [])
                        if self.selection['request_id'] not in ids:
                            ids.append(self.selection['request_id'])
                        self.save()
                        self.history('action_interrupted', request_id=self.selection['request_id'], remote_or_execution_result='UNKNOWN', automatic_resume=False)
                    self.report_error(exc)
                    self.quit = True
                except ActionError as exc:
                    self.report_error(exc)
                except (ValueError, OSError, KeyError) as exc:
                    self.history('ui_error', error_type=type(exc).__name__)
                    self.output('当前动作未完成；请用 /status 只读检查。没有自动重试。')
        except (KeyboardInterrupt, EOFError):
            self.output('\n已退出交互终端；未自动提交输入或重发动作。')
        except ActionError as exc:
            self.report_error(exc)
            return 1
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='v12', description='V12 中文持续对话终端。在 CMD 中 cd /d D:\\mbse\\week13 后输入 v12。')
    parser.add_argument('--console-root', type=Path, default=CONSOLE_ROOT, help='仅保存终端选择/历史的目录；不改变案例或业务账本')
    parser.add_argument('--execution-timeouts', type=json.loads, help='新请求完整进程超时JSON；展示并交由既有预算授权校验')
    parser.add_argument('--delegated-approval-basis', help='代理代已授权流程输入确认时引用的真实既有授权记录；不冒称人工亲审方案')
    args = parser.parse_args(argv)
    # Product credentials are intentionally neither loaded nor inspected here.
    return Console(console_root=args.console_root, execution_timeouts=args.execution_timeouts, delegated_approval_basis=args.delegated_approval_basis).loop()


if __name__ == '__main__':
    raise SystemExit(main())
