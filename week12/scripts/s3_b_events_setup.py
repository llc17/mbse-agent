"""Prepare the revised finite executor without changing earlier evidence."""
from pathlib import Path
import ast


def main():
    source = Path('week12/scripts/s3_b_auto_repair.py').read_text(encoding='utf-8')
    changes = [
        ("BASE = ROOT/'results/s3-b-auto-repair-20260923-01'", "BASE = ROOT/'results/s3-b-auto-repair-20260923-02'"),
        ("PRIOR = ROOT/'results/s3-b-connector-repair-20260923-01'", "PRIOR = ROOT/'results/s3-b-auto-repair-20260923-01/round-02'"),
        ("TEST = ROOT/'tests/test_component_physics_event_range.py'", "TEST = ROOT/'tests/test_component_physics_scalar_events.py'\nRUNNER = ROOT/'src/component_physics/runner.py'\nGUARD = ROOT/'src/component_physics/event_codegen.py'\nGUARD_TEST = ROOT/'tests/test_component_physics_event_codegen.py'\n_RUN_STARTED = None"),
        ("'user_message': '修改为自动修复，这种小错误，不要每次都让我来继续'", "'user_message': '改修不合理的地方，继续完成自动修复'"),
        ("'new_attempt_limit': 16, 'new_local_limit': 8", "'new_attempt_limit': 30, 'new_local_limit': 12"),
        ("'historical_attempts': 6, 'historical_local': 16, 'historical_simulations': 2", "'historical_attempts': 16, 'historical_local': 21, 'historical_simulations_started': 12, 'historical_simulations_completed': 11,\n                    'previous_unused_allowance_retired': True, 'preparation_not_blocked_by_execution_budget': True,\n                    'previous_checkpoint': row(ROOT/'results/s3-b-auto-repair-20260923-01/checkpoint.json')"),
        ("previous = read(PRIOR/'implementation-manifest.json')", "previous = read(PRIOR/'source-manifest.json')"),
        ("changed = {PACKAGE.resolve(), HARNESS.resolve()}", "changed = {PACKAGE.resolve(), RUNNER.resolve(), POLICY_DOCUMENT.resolve()}"),
        ("for path in (PACKAGE, HARNESS):", "for path in (PACKAGE, RUNNER, POLICY_DOCUMENT):"),
        ("if row(BASE/'before'/path.name)['sha256'] != original['sha256']:", "backup_name = '自动修复执行补充.md' if path == POLICY_DOCUMENT else path.name\n        if row(BASE/'before'/backup_name)['sha256'] != original['sha256']:"),
        ("require_dependency_hashes(read(PRIOR/'historical-manifest.json'))", "require_dependency_hashes(read(ROOT/'results/s3-b-connector-repair-20260923-01/historical-manifest.json'))\n    require_dependency_hashes(read(PRIOR/'diagnosis-evidence-manifest.json'))"),
        ("reuse = read(PRIOR/'reuse-input-manifest.json')", "reuse = read(PRIOR/'reuse-manifest.json')"),
        ("{PACKAGE.resolve(), HARNESS.resolve(), TEST.resolve()}", "{PACKAGE.resolve(), RUNNER.resolve(), GUARD.resolve(), TEST.resolve(), GUARD_TEST.resolve()}"),
        ("for path in (PACKAGE, HARNESS, TEST)):", "for path in (PACKAGE, RUNNER, GUARD, TEST, GUARD_TEST)):"),
        ("for path in (HARNESS, TEST, Path(__file__)):", "for path in (TEST, GUARD_TEST, RUNNER, GUARD, Path(__file__)):"),
        ("not test.startswith('week12.tests.test_component_physics_event_range.')", "not test.startswith(('week12.tests.test_component_physics_scalar_events.', 'week12.tests.test_component_physics_event_codegen.'))"),
        ("if (BASE/'execution-start.json').exists() and remaining_time(policy) <= 0:", "if remaining_time(policy) <= 0:"),
        ("frozen = unchanged+[row(p) for p in (PACKAGE, HARNESS, TEST, Path(__file__), POLICY_DOCUMENT, BASE/'policy.json')]", "extras = [PACKAGE, RUNNER, GUARD, TEST, GUARD_TEST, Path(__file__), POLICY_DOCUMENT, BASE/'policy.json']\n    helper = ROOT/'tests/modelica_equation_harness.py'\n    if helper.exists():\n        extras.append(helper)\n    frozen = unchanged+[row(p) for p in extras]"),
        ("historical_attempts=6+attempts,", "historical_attempts=16+attempts,"),
        ("proposed_cumulative_attempt_ceiling=22, local_proposed_cumulative_ceiling=24,", "proposed_cumulative_attempt_ceiling=46, local_proposed_cumulative_ceiling=33,"),
        ("def run(number):\n    policy", "def run(number):\n    global _RUN_STARTED\n    policy"),
        ("approval['historical_attempts'] != 6+attempts", "approval['historical_attempts'] != 16+attempts"),
        ("result = {'status': 'RUNNING'", "_RUN_STARTED = time.monotonic()\n    result = {'status': 'RUNNING'"),
        ("ledger.prepare(historical_attempts=6+attempts)", "ledger.prepare(historical_attempts=16+attempts)"),
        ("'cumulative_slot': 17+local_used", "'cumulative_slot': 22+local_used"),
        ("'cumulative_slot': 7+attempts", "'cumulative_slot': 17+attempts"),
        ("('scenario', 'status', 'model_check', 'compile', 'simulation', 'physical', 'numeric', 'error')", "('scenario', 'status', 'model_check', 'compile', 'event_codegen', 'simulation', 'physical', 'numeric', 'error')"),
        ("cumulative_attempts=6+attempts,", "cumulative_attempts=16+attempts,"),
        ("new_local_total=local_used, cumulative_local=16+local_used)", "new_local_total=local_used, cumulative_local=21+local_used,\n                      execution_elapsed_seconds=time.monotonic()-_RUN_STARTED)"),
        ("        (BASE/'active.lock').unlink()", "        _RUN_STARTED = None\n        (BASE/'active.lock').unlink()"),
    ]
    for old, new in changes:
        if source.count(old) != 1:
            raise ValueError('Unexpected old executor shape: '+old)
        source = source.replace(old, new)
    a = source.index('    stamp = BASE/', source.index('def remaining_time'))
    b = source.index('\n\n\ndef seal', a)
    source = source[:a]+'''    elapsed = sum(read(p).get('execution_elapsed_seconds', 0) for p in BASE.glob('round-*/batch-result.json'))
    if _RUN_STARTED is not None:
        elapsed += time.monotonic()-_RUN_STARTED
    return policy['wall_seconds']-elapsed
'''+source[b:]
    a = source.index('    # Declarations, parameters, equations')
    b = source.index('    for path in (TEST, GUARD_TEST, RUNNER, GUARD, Path(__file__)):', a)
    source = source[:a]+'''    contact_source = re.search(r'  model PolylineWheelContact\\b.*?  end PolylineWheelContact;', new, re.S).group()
    if 'noEvent' in contact_source or '\\n  algorithm' in contact_source:
        raise ValueError('Contact must retain equation events without the failing algorithm loop form')
'''+source[b:]
    ast.parse(source)
    with Path('week12/scripts/s3_b_auto_events.py').open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(source)
    print('Revised bounded executor written and syntax parsed; no tests or Modelica run.')


if __name__ == '__main__':
    main()
