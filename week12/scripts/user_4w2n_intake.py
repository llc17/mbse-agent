"""Read only the user supplied bundle; preserve byte copies and OOXML evidence."""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '6轮上下楼仿真'
OUT = ROOT / 'results/user-4w2n/original-20260922-01'
SNAPSHOT = ROOT / 'models/user-4w2n/source-v1'

def main():
    OUT.mkdir(parents=True, exist_ok=False)
    SNAPSHOT.mkdir(parents=True, exist_ok=False)
    manifest = []
    for source in sorted(SOURCE.rglob('*')):
        if not source.is_file() or source.name.startswith('~$'):
            continue
        relative = source.relative_to(SOURCE)
        target = SNAPSHOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        shutil.copy2(source, target)
        after = hashlib.sha256(target.read_bytes()).hexdigest()
        assert before == after
        manifest.append(dict(path=relative.as_posix(), bytes=source.stat().st_size, sha256=before))
        if source.suffix == '.docx':
            extract = OUT / 'documents' / source.stem
            extract.mkdir(parents=True)
            with zipfile.ZipFile(target) as archive:
                xml = ET.fromstring(archive.read('word/document.xml'))
                ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
                      'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
                      'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
                rels = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
                relmap = {e.attrib['Id']: e.attrib['Target'] for e in rels}
                paragraphs = []
                for i, p in enumerate(xml.findall('.//w:p', ns)):
                    texts = ''.join(e.text or '' for e in p.iter() if e.tag.endswith('}t'))
                    pics = [relmap.get(e.attrib.get('{'+ns['r']+'}embed'), '?') for e in p.findall('.//a:blip', ns)]
                    if texts or pics:
                        paragraphs.append(f'P{i:04d}: {texts}' + (f' [IMAGES: {pics}]' if pics else ''))
                (extract / 'paragraphs.txt').write_text('\n'.join(paragraphs), encoding='utf-8')
                for name in archive.namelist():
                    if name.startswith('word/media/') and not name.endswith('/'):
                        (extract / Path(name).name).write_bytes(archive.read(name))
    (OUT / 'source-manifest.json').write_text(json.dumps(dict(source=str(SOURCE),snapshot=str(SNAPSHOT),files=manifest,excluded=['Word lock file ~$动力学论文初稿.docx']),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
