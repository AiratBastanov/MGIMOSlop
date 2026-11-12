"""Upload limits before CSRF parses multipart; no public media directory."""
import html
import re
import types
import unicodedata
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http.multipartparser import MultiPartParser
from django.utils.datastructures import ImmutableList

KINDS={'pdf':'application/pdf','docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','txt':'text/plain','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}

def safe_name(name):
    name=html.unescape(name)
    if (not 1<=len(name)<=160 or name!=name.strip() or name.startswith('.') or '..' in name
            or any(c in name for c in '/\\:<>"|?*\r\n')
            or any(unicodedata.category(c).startswith('C') for c in name)
            or re.match(r'^(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)',name,re.I)
            or re.search(r'\.(?:exe|com|bat|cmd|ps1|js|vbs|scr|dll)(?:\.|$)',name,re.I)):
        raise ValueError('unsafe filename')
    extension=name.rsplit('.',1)[-1].lower()
    kind='jpeg' if extension in ('jpg','jpeg') else extension
    if kind not in KINDS: raise ValueError('unsupported extension')
    return name,kind

class StrictParser(MultiPartParser):
    def sanitize_file_name(self,name):
        try: return safe_name(name)[0]
        except ValueError: raise SuspiciousOperation('Недопустимое имя или расширение вложения.')

class BoundedUpload(FileUploadHandler):
    chunk_size=64*1024
    def __init__(self,request):
        super().__init__(request);self.total=0;self.count=0
    def new_file(self,*args,**kwargs):
        super().new_file(*args,**kwargs);self.count+=1;self.current=0
        if self.field_name!='files' or self.count>settings.MATERIAL_ATTACHMENT_COUNT:
            self.request.material_upload_error=True;raise StopUpload(connection_reset=True)
    def receive_data_chunk(self,raw_data,start):
        self.total+=len(raw_data);self.current+=len(raw_data)
        if self.current>settings.MATERIAL_ATTACHMENT_FILE_BYTES or self.total>settings.MATERIAL_ATTACHMENT_TOTAL_BYTES:
            self.request.material_upload_error=True;raise StopUpload(connection_reset=True)
        return raw_data
    def file_complete(self,file_size): return None

class MaterialUploadMiddleware:
    def __init__(self,get_response): self.get_response=get_response
    def __call__(self,request):
        if request.method=='POST' and (re.fullmatch(r'/study/materials/(?:[1-9][0-9]*/)?editor/',request.path) or re.fullmatch(r'/teacher/publications/(?:new|[1-9][0-9]*)/editor/',request.path)):
            request.upload_handlers.insert(0,BoundedUpload(request))
            def parse(this,META,post_data):
                this.upload_handlers=ImmutableList(this.upload_handlers,warning='Upload already started.')
                return StrictParser(META,post_data,this.upload_handlers,this.encoding).parse()
            request.parse_file_upload=types.MethodType(parse,request)
        return self.get_response(request)
