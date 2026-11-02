"""Optional operation progress, separate from product replies and evidence."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def emit_progress(stage, message, **details):
    target = os.environ.get('MBSE_V12_PROGRESS_FILE')
    if not target:
        return
    path = Path(target)
    request_id=details.get('request_id') or os.environ.get('MBSE_V12_REQUEST_ID')
    if request_id:details['request_id']=request_id
    # The operation owns a unique file. Retain its already-bound result
    # pointer across later explanation stages so a short event cannot be lost
    # between UI polls. Never carry a pointer into a different request.
    try:
        previous=json.loads(path.read_text(encoding='utf8'))
        prior=previous.get('details') or {}
        if request_id and prior.get('request_id')==request_id and prior.get('result_ready') and 'result_ready' not in details:
            details['result_ready']=prior['result_ready']
    except (OSError,ValueError,TypeError,AttributeError):pass
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    value = dict(schema='v12-operation-progress-1', stage=stage, message=message,
                 updated_at_utc=datetime.now(timezone.utc).isoformat(), details=details)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf8')
        temporary.replace(path)
    except OSError:
        # A vanished UI or unavailable progress file must not change execution.
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
