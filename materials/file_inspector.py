"""Isolated, bounded content inspection; no network, rendering or extraction."""
import io
import os
import re
import stat
import sys
import unicodedata
import warnings
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from xml.parsers import expat


def safe_xml(raw):
    if len(raw)>4*1024*1024: raise ValueError('xml size')
    parser=expat.ParserCreate();depth=0;count=0
    def forbidden(*args): raise ValueError('DTD and entities are forbidden')
    def start(*args):
        nonlocal depth,count
        depth+=1;count+=1
        if depth>64 or count>50000: raise ValueError('xml complexity')
    def end(*args):
        nonlocal depth
        depth-=1
    parser.StartDoctypeDeclHandler=forbidden;parser.EntityDeclHandler=forbidden;parser.ExternalEntityRefHandler=forbidden
    parser.StartElementHandler=start;parser.EndElementHandler=end
    parser.Parse(raw,True)  # Encoding-aware rejection, including UTF-16 DTD.
    return ET.fromstring(raw)


def inspect(kind, raw):
    if not raw or len(raw)>10*1024*1024:
        raise ValueError('size')
    if kind in ('jpeg','png','webp'):
        from PIL import Image
        Image.MAX_IMAGE_PIXELS=20_000_000
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format!={'jpeg':'JPEG','png':'PNG','webp':'WEBP'}[kind] or getattr(picture,'n_frames',1)!=1:
                    raise ValueError('image')
                if picture.width*picture.height>20_000_000: raise ValueError('pixels')
                picture.verify()
            with Image.open(io.BytesIO(raw)) as picture: picture.load()
    elif kind=='txt':
        text=raw.decode('utf-8-sig')
        if any(unicodedata.category(c) in ('Cc','Cs') and c not in '\n\r\t' for c in text): raise ValueError('text')
        if re.search(r'<\s*(?:!doctype|html|script|svg)|^\s*#!|^\s*(?:MZ|\x7fELF)',text,re.I): raise ValueError('active text')
    elif kind=='docx':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos=archive.infolist()
            if not 3<=len(infos)<=300: raise ValueError('entries')
            names=[i.filename for i in infos]
            if len(set(names))!=len(names) or sum(i.file_size for i in infos)>40*1024*1024: raise ValueError('container')
            if not {'[Content_Types].xml','_rels/.rels','word/document.xml'}<=set(names): raise ValueError('docx')
            for info in infos:
                name=info.filename
                if (name.startswith('/') or '\\' in name or ':' in name or '..' in PurePosixPath(name).parts
                        or info.flag_bits&1 or info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED)
                        or stat.S_ISLNK(info.external_attr>>16) or info.file_size>10*1024*1024
                        or info.file_size>max(1024*1024,info.compress_size*100)):
                    raise ValueError('zip member')
                if re.search(r'vba|activex|embeddings|\.bin$|\.exe$|\.html?$|\.svg$',name,re.I): raise ValueError('active member')
                if not (name.endswith(('.xml','.rels','/')) or re.fullmatch(r'word/media/[^/]+\.(?:png|jpe?g|webp)',name,re.I)):
                    raise ValueError('unsupported member')
                content=archive.read(info)  # CRC and bounded expanded length; never extract.
                if name.endswith(('.xml','.rels')):
                    root=safe_xml(content)
                    if name=='word/document.xml' and root.tag!='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document': raise ValueError('document')
                    if name=='[Content_Types].xml':
                        namespace='{http://schemas.openxmlformats.org/package/2006/content-types}'
                        if root.tag!=namespace+'Types': raise ValueError('content types root')
                        main=[n for n in root if n.tag==namespace+'Override' and n.attrib.get('PartName')=='/word/document.xml']
                        if len(main)!=1 or main[0].attrib.get('ContentType')!='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml': raise ValueError('document content type')
                        types=[n.attrib.get('ContentType','') for n in root]
                        if 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml' not in types: raise ValueError('main type')
                        if any('macro' in t.lower() or 'oleObject' in t for t in types): raise ValueError('active type')
                    if name.endswith('.rels'):
                        namespace='{http://schemas.openxmlformats.org/package/2006/relationships}'
                        if root.tag!=namespace+'Relationships' or any(n.tag!=namespace+'Relationship' for n in root): raise ValueError('relationships root')
                        if name=='_rels/.rels':
                            main=[n for n in root if n.attrib.get('Type')=='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument']
                            if len(main)!=1 or main[0].attrib.get('Target','').lstrip('/')!='word/document.xml': raise ValueError('package document relationship')
                        for rel in root:
                            target=rel.attrib.get('Target','')
                            if rel.attrib.get('TargetMode','').lower()=='external' or re.match(r'^[a-z]+:',target,re.I) or '\\' in target:
                                raise ValueError('external relationship')
                elif name.startswith('word/media/'):
                    kind=name.rsplit('.',1)[-1].lower();inspect('jpeg' if kind=='jpg' else kind,content)
    elif kind=='pdf':
        from pypdf import PdfReader
        from pypdf.generic import IndirectObject, DictionaryObject, ArrayObject
        if not raw.startswith(b'%PDF-') or not raw.rstrip().endswith(b'%%EOF'): raise ValueError('pdf header')
        reader=PdfReader(io.BytesIO(raw),strict=True,root_object_recovery_limit=0,xref_stream_maximum_entries=50000)
        if reader.is_encrypted or not 1<=len(reader.pages)<=1000: raise ValueError('pdf pages')
        forbidden={'/JS','/JavaScript','/Launch','/OpenAction','/AA','/EmbeddedFiles','/EmbeddedFile','/RichMedia','/XFA','/SubmitForm','/ImportData','/GoToR','/AcroForm'}
        seen=set(); stack=[reader.trailer]; count=0
        while stack:
            value=stack.pop();count+=1
            if count>100000: raise ValueError('pdf complexity')
            if isinstance(value,IndirectObject):
                key=(value.idnum,value.generation)
                if key in seen: continue
                seen.add(key);stack.append(value.get_object())
            elif isinstance(value,DictionaryObject):
                if forbidden.intersection(map(str,value.keys())) or str(value.get('/S','')) in forbidden: raise ValueError('active pdf')
                stack.extend(value.values())
            elif isinstance(value,ArrayObject): stack.extend(value)
    else: raise ValueError('type')


def limit_memory():
    """OS limit supplements input/container limits and the parent's wall timeout."""
    if os.name!='nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        return
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),('min_ws',c.c_size_t),('max_ws',c.c_size_t),('processes',w.DWORD),('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class Extended(c.Structure):
        _fields_=[('basic',Basic),('io',c.c_uint64*6),('process_memory',c.c_size_t),('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    handle=kernel.CreateJobObjectW(None,None)
    limits=Extended();limits.basic.flags=0x100;limits.process_memory=512*1024*1024
    if not handle or not kernel.SetInformationJobObject(handle,9,c.byref(limits),c.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,w.HANDLE(-1)):
        raise RuntimeError('cannot bound inspector')


if __name__=='__main__':
    try:
        limit_memory()
        inspect(sys.argv[1],sys.stdin.buffer.read(10*1024*1024+1))
    except Exception:
        sys.exit(1)
    sys.stdout.write('valid')
