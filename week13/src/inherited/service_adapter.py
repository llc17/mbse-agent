"""V11 immutable manifest verification, adapted to the new write root."""
import hashlib,json
from pathlib import Path

def file_sha256(path):
    """Hash complete content with bounded memory; never trust stat as a hash."""
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def _verify_manifest(path, expected_sha, root):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise ValueError('manifest hash mismatch')
    manifest=json.loads(raw);files=manifest.get('files')
    if not isinstance(files,dict) or not files:raise ValueError('manifest must enumerate immutable evidence files')
    for name,record in files.items():
        if not isinstance(name,str) or not name or Path(name).is_absolute() or '..' in Path(name).parts or not isinstance(record,dict):raise ValueError('unsafe evidence manifest path')
        candidate=(Path(root)/name).resolve()
        if not candidate.is_relative_to(Path(root).resolve()):raise ValueError('evidence escapes task')
        if file_sha256(candidate)!=record.get('sha256') or candidate.stat().st_size!=record.get('bytes'):raise ValueError('evidence artifact hash mismatch')
    return manifest
