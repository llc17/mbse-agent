"""Small explicit reference snippets for ordinary product CLI requests."""
import hashlib,json
from .contracts import inside_week12

def load_modeling_reference(path):
    path=inside_week12(path);raw=path.read_bytes()
    if len(raw)>40000:raise ValueError('modeling reference context too large')
    value=json.loads(raw)
    if not isinstance(value,dict) or set(value)!={'schema','purpose','limitations','items'} or value['schema']!='v11-artifact-modeling-reference-1' or not isinstance(value['purpose'],str) or not isinstance(value['limitations'],list) or not all(isinstance(v,str) for v in value['limitations']) or not isinstance(value['items'],list) or not 1<=len(value['items'])<=8:raise ValueError('invalid modeling reference manifest')
    for item in value['items']:
        if not isinstance(item,dict) or set(item)!={'path','source_sha256','line_start','line_end','excerpt_sha256','excerpt','purpose','limitations'}:raise ValueError('invalid modeling reference item')
        source=inside_week12(item['path']);source_raw=source.read_bytes()
        if hashlib.sha256(source_raw).hexdigest()!=item['source_sha256']:raise ValueError('modeling reference source drift')
        lines=source_raw.decode('utf-8-sig').splitlines()
        start,end=item['line_start'],item['line_end']
        if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):raise ValueError('invalid modeling reference range')
        excerpt='\n'.join(lines[start-1:end])
        if item['excerpt']!=excerpt or hashlib.sha256(excerpt.encode('utf8')).hexdigest()!=item['excerpt_sha256']:raise ValueError('modeling reference excerpt drift')
        if not isinstance(item['purpose'],str) or not isinstance(item['limitations'],list) or not all(isinstance(v,str) for v in item['limitations']):raise ValueError('invalid modeling reference scope')
    return dict(manifest_path=str(path),manifest_sha256=hashlib.sha256(raw).hexdigest(),kind='MODELING_REFERENCE_NOT_USER_FACTS_OR_VEHICLE_ACCEPTANCE',**value)
