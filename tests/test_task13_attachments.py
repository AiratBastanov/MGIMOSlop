"""TASK13: actual multipart, PostgreSQL fences, immutable exact file versions."""
import hashlib
import io
import json
import uuid
import zipfile
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction, connection
from django.test import Client, TransactionTestCase, SimpleTestCase, override_settings
from accounts.access import current_actor
from materials.models import Material, MaterialAttachment, MaterialOperation, RevisionAttachment, DraftAttachment
from materials.selectors import current_policy
from materials.file_inspector import inspect
from tests import test_task10_materials as previous


def image_bytes(fmt='PNG'):
    from PIL import Image
    out=io.BytesIO();Image.new('RGB',(8,6),'navy').save(out,format=fmt);return out.getvalue()

def pdf_bytes():
    from pypdf import PdfWriter
    out=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=300,height=400);writer.write(out);return out.getvalue()

def docx_bytes(external=False):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Синтетический пример</w:t></w:r></w:p></w:body></w:document>')
        if external: archive.writestr('word/_rels/document.xml.rels','<Relationships><Relationship TargetMode="External" Target="https://example.invalid/file"/></Relationships>')
    return out.getvalue()


class AttachmentTests(TransactionTestCase):
    login=previous.MaterialTests.login;request=previous.MaterialTests.request;page=previous.MaterialTests.page;post=previous.MaterialTests.post;marker=previous.MaterialTests.marker
    policy=previous.MaterialTests.policy;review=previous.MaterialTests.review
    def setUp(self):
        previous.MaterialTests.setUp(self)
        self.policy(attachments_allowed=True,attachment_types=['pdf','docx','txt','jpeg','png','webp'],attachment_count=5,attachment_file_mib=10,attachment_total_mib=25)

    def send(self,row=None,action='save',files=(),retained=(),key=None,version=None,**extra):
        if row: row.refresh_from_db()
        fields=dict(context=current_actor(self.request()).marker,operation=str(key or uuid.uuid4()),action=action,
            assignment=str(self.assignment.pk),title='TASK13 синтетический материал',mode='text',content='Текст для проверки файлов',
            expected_version=str(version if version is not None else row.draft_version if row else 0),
            expected_policy=str(current_policy(self.assignment.pk).version),attachment_ids=json.dumps(list(retained)),
            csrfmiddlewaretoken=self.a.cookies['csrftoken'].value)
        fields.update(extra)
        fields['files']=[SimpleUploadedFile(name,raw,mime) for name,raw,mime in files]
        return self.a.post(f'/study/materials/{str(row.pk)+"/" if row else ""}editor/',fields,HTTP_X_CSRFTOKEN=self.a.cookies['csrftoken'].value)

    def test_single_multiple_exact_teacher_history_and_private_correction(self):
        first=('first.txt',b'first text','text/plain')
        response=self.send(files=[first]);self.assertEqual(response.status_code,200,response.content)
        row=Material.objects.get(pk=response.json()['material']['id']);saved=response.json()['material']['attachments']
        self.assertEqual(len(saved),1)
        self.assertEqual(b''.join(self.a.get(saved[0]['url']).streaming_content),first[1])
        self.assertEqual(self.teacher.get(saved[0]['url']).status_code,403)
        mixed=[('picture.png',image_bytes(),'image/png'),('report.pdf',pdf_bytes(),'application/pdf'),('report.docx',docx_bytes(),'application/octet-stream')]
        result=self.send(row,files=mixed,retained=[saved[0]['id']]);self.assertEqual(result.status_code,200,result.content)
        files=result.json()['material']['attachments'];self.assertEqual(len(files),4)
        reloaded=self.a.get(f'/study/materials/{row.pk}/editor/').json();self.assertEqual(files,reloaded['material']['attachments'])
        submitted=self.send(row,action='submit',retained=[f['id'] for f in files]);self.assertEqual(submitted.status_code,200,submitted.content)
        revision=row.revisions.get();self.assertEqual(revision.attachment_manifest,[f['id'] for f in files])
        expected=[first[1]]+[v[1] for v in mixed]
        urls=submitted.json()['material']['revisions'][0]['attachments']
        for file,raw in zip(urls,expected):
            download=self.teacher.get(file['url']);self.assertEqual(download.status_code,200)
            self.assertEqual(hashlib.sha256(b''.join(download.streaming_content)).hexdigest(),hashlib.sha256(raw).hexdigest())
            self.assertIn('attachment;',download['Content-Disposition']);self.assertEqual(download['X-Content-Type-Options'],'nosniff');self.assertIn('no-store',download['Cache-Control'])
        self.review(revision)
        corrected=self.send(row,action='correct');self.assertEqual(corrected.status_code,200,corrected.content)
        replacement=self.send(row,files=[('private.txt',b'private later draft','text/plain')]);self.assertEqual(replacement.status_code,200,replacement.content)
        private=replacement.json()['material']['attachments'][0]
        self.assertNotContains(self.teacher.get(f'/teacher/materials/{row.pk}/'),'private.txt')
        self.assertEqual(self.teacher.get(private['url']).status_code,403)
        self.assertEqual(self.teacher.get(f'/study/materials/revisions/{revision.pk}/files/{private["id"]}/').status_code,404)
        sent2=self.send(row,action='submit',retained=[private['id']]);self.assertEqual(sent2.status_code,200,sent2.content)
        revision.refresh_from_db();self.assertEqual(revision.attachment_manifest,[f['id'] for f in files]);self.assertEqual(revision.review.feedback,'Добавьте пояснение к примеру.')
        self.assertEqual(MaterialAttachment.objects.count(),5)

    def test_retry_create_save_submit_and_changed_key_payload(self):
        key=uuid.uuid4();file=('a.txt',b'upload retry','text/plain')
        first=self.send(files=[file],key=key);self.assertEqual(first.status_code,200,first.content)
        repeat=self.send(files=[file],key=key);self.assertEqual(repeat.status_code,200,repeat.content)
        self.assertEqual(Material.objects.count(),1);self.assertEqual(MaterialAttachment.objects.count(),1)
        changed=self.send(files=[('a.txt',b'changed bytes','text/plain')],key=key);self.assertEqual(changed.status_code,409)
        row=Material.objects.get();version=row.draft_version;retained=[str(MaterialAttachment.objects.get().pk)];key=uuid.uuid4()
        self.assertEqual(self.send(row,key=key,retained=retained,version=version).status_code,200)
        self.assertEqual(self.send(row,key=key,retained=retained,version=version).status_code,200)
        row.refresh_from_db();version=row.draft_version;key=uuid.uuid4()
        self.assertEqual(self.send(row,'submit',key=key,retained=retained,version=version).status_code,200)
        self.assertEqual(self.send(row,'submit',key=key,retained=retained,version=version).status_code,200)
        self.assertEqual(row.revisions.count(),1);self.assertEqual(MaterialOperation.objects.count(),3)

    def test_every_file_atomic_rejection_counts_sizes_types_policy(self):
        good=('ok.txt',b'valid text','text/plain')
        cases=[([good,('bad.png',b'not an image','image/png')],400),([good]*6,400),
               ([('big.txt',b'a'*(10*1024*1024+1),'text/plain')],400),
               ([('a.txt',b'a'*(9*1024*1024),'text/plain')]*3,400)]
        for files,code in cases:
            with self.subTest(names=[v[0] for v in files]):
                response=self.send(files=files);self.assertEqual(response.status_code,code,response.content[:200])
                self.assertFalse(Material.objects.exists());self.assertFalse(MaterialAttachment.objects.exists());self.assertFalse(MaterialOperation.objects.exists())
        self.policy(attachments_allowed=False)
        response=self.send(files=[good]);self.assertEqual(response.status_code,400);self.assertFalse(Material.objects.exists())

    def test_foreign_download_upload_csrf_and_retained_id_fencing(self):
        response=self.send(files=[('a.txt',b'private data','text/plain')]);self.assertEqual(response.status_code,200,response.content)
        row=Material.objects.get();file=response.json()['material']['attachments'][0]
        self.login(self.b,'student_b')
        self.assertEqual(self.b.get(file['url']).status_code,404)
        self.assertIn(Client().get(file['url']).status_code,[302,401])
        self.assertEqual(self.a.post(f'/study/materials/{row.pk}/editor/',{'action':'save'}).status_code,403)
        response=self.send(retained=[file['id']]);self.assertEqual(response.status_code,404)
        self.assertEqual(Material.objects.count(),1)
        self.assertEqual(self.send(row,retained=[file['id']],version=row.draft_version+2).status_code,409)

    def test_raw_traversal_and_unknown_file_field_are_rejected(self):
        boundary='task13boundary'
        token=self.a.cookies['csrftoken'].value
        for name in ['../../bad.pdf','x\\bad.txt','bad\x01.txt','run.exe.txt']:
            raw=(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: text/plain\r\n\r\ntext\r\n--{boundary}--\r\n').encode()
            response=self.a.post('/study/materials/editor/',raw,content_type=f'multipart/form-data; boundary={boundary}',HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code,400,(name,response.status_code))
        self.assertFalse(Material.objects.exists())

    def test_database_immutable_blobs_exact_links_and_late_append(self):
        response=self.send(files=[('one.txt',b'one','text/plain'),('two.txt',b'two','text/plain')]);self.assertEqual(response.status_code,200,response.content)
        row=Material.objects.get();files=list(MaterialAttachment.objects.all())
        with self.assertRaises(IntegrityError),transaction.atomic(): MaterialAttachment.objects.filter(pk=files[0].pk).update(name='changed.txt')
        response=self.send(row,action='submit',retained=[str(f.pk) for f in files]);self.assertEqual(response.status_code,200,response.content)
        revision=row.revisions.get()
        with self.assertRaises(IntegrityError),transaction.atomic(): RevisionAttachment.objects.filter(revision=revision).delete()
        with self.assertRaises(IntegrityError),transaction.atomic(): RevisionAttachment.objects.create(revision=revision,attachment=files[0],position=4)
        with self.assertRaises(IntegrityError),transaction.atomic(): MaterialOperation.objects.all().update(fingerprint='0'*64)

    def test_exact_limits_retained_aggregate_and_effective_policy(self):
        files=[('ten-a.txt',b'a'*(10*1024*1024),'text/plain'),('ten-b.txt',b'b'*(10*1024*1024),'text/plain'),('five.txt',b'c'*(5*1024*1024),'text/plain')]
        result=self.send(files=files);self.assertEqual(result.status_code,200,result.content)
        row=Material.objects.get();retained=[f['id'] for f in result.json()['material']['attachments']]
        before=row.draft_version
        result=self.send(row,retained=retained,files=[('extra.txt',b'x','text/plain')]);self.assertEqual(result.status_code,400)
        row.refresh_from_db();self.assertEqual(row.draft_version,before);self.assertEqual(MaterialAttachment.objects.count(),3)
        with override_settings(MATERIAL_ATTACHMENT_COUNT=2,MATERIAL_ATTACHMENT_FILE_BYTES=4*1024*1024,MATERIAL_ATTACHMENT_TOTAL_BYTES=8*1024*1024):
            policy=self.a.get(f'/study/materials/{row.pk}/editor/').json()['assignments'][0]['policy']
            self.assertEqual((policy['attachment_count'],policy['attachment_file_limit'],policy['attachment_total_limit']),(2,4*1024*1024,8*1024*1024))
        # Removing pending/saved files really replaces the set and releases only unreferenced bytes.
        result=self.send(row,retained=[retained[0]]);self.assertEqual(result.status_code,200,result.content)
        self.assertEqual(MaterialAttachment.objects.count(),1)

    def test_context_revoked_after_inspection_rolls_back_everything(self):
        from materials.attachments import inspect_upload
        from materials.services import set_assignment_active
        def revoke(file):
            result=inspect_upload(file);set_assignment_active(self.assignment.pk,False,operator='task13-isolated-revoke');return result
        with patch('materials.attachments.inspect_upload',side_effect=revoke):
            response=self.send(files=[('a.txt',b'valid text','text/plain')])
        self.assertIn(response.status_code,[403,409]);self.assertFalse(Material.objects.exists());self.assertFalse(MaterialAttachment.objects.exists());self.assertFalse(MaterialOperation.objects.exists())

    def test_canonical_duplicate_unknown_field_staff_unassigned_teacher(self):
        from django.contrib.auth import get_user_model
        from accounts.models import Identity
        from studies.models import Instructor
        result=self.send(files=[('one.txt',b'one','text/plain')]);row=Material.objects.get();file=result.json()['material']['attachments'][0]
        self.assertEqual(self.send(row,retained=[file['id'],file['id'].upper()]).status_code,409)
        self.assertEqual(MaterialAttachment.objects.count(),1)
        self.assertEqual(self.send(row,extra_file=SimpleUploadedFile('other.txt',b'other')).status_code,400)
        sent=self.send(row,action='submit',retained=[file['id']]);url=sent.json()['material']['revisions'][0]['attachments'][0]['url']
        staff=Client(enforce_csrf_checks=True);self.login(staff,'staff_demo');self.assertEqual(staff.get(url).status_code,403)
        user=get_user_model().objects.create_user(username='task13_unassigned',password='isolated-test-only')
        Identity.objects.create(user=user,role='teacher',instructor=Instructor.objects.exclude(pk=self.assignment.teacher.instructor_id).first())
        self.credentials[user.username]='isolated-test-only';teacher=Client(enforce_csrf_checks=True);self.login(teacher,user.username)
        self.assertEqual(teacher.get(url).status_code,404)

    def test_concurrent_identical_retry_and_save_submit_version_fence(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        marker=current_actor(self.request()).marker;policy=current_policy(self.assignment.pk).version
        def race(path,payloads):
            barrier=Barrier(2)
            def worker(payload):
                close_old_connections()
                try:
                    client=Client(enforce_csrf_checks=True);client.cookies=self.a.cookies.copy();barrier.wait(timeout=10)
                    return client.post(path,{**payload,'files':[SimpleUploadedFile('race.txt',b'race','text/plain')]},HTTP_X_CSRFTOKEN=self.a.cookies['csrftoken'].value).status_code
                finally:close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as executor:return list(executor.map(worker,payloads))
        payload=dict(context=marker,operation=str(uuid.uuid4()),action='save',assignment=str(self.assignment.pk),title='Concurrent',mode='text',content='race text',expected_version='0',expected_policy=str(policy),attachment_ids='[]',csrfmiddlewaretoken=self.a.cookies['csrftoken'].value)
        self.assertEqual(race('/study/materials/editor/',[payload,payload]),[200,200]);self.assertEqual(Material.objects.count(),1);self.assertEqual(MaterialAttachment.objects.count(),1);self.assertEqual(MaterialOperation.objects.count(),1)
        row=Material.objects.get();payload.update(expected_version=str(row.draft_version),operation=str(uuid.uuid4()))
        submit={**payload,'operation':str(uuid.uuid4()),'action':'submit','content':'exact concurrent submission'}
        statuses=race(f'/study/materials/{row.pk}/editor/',[payload,submit]);self.assertEqual(sorted(statuses),[200,409]);row.refresh_from_db()
        self.assertEqual(MaterialAttachment.objects.count(),1);self.assertEqual(MaterialOperation.objects.count(),2)
        if row.submission_count:
            revision=row.revisions.get();self.assertEqual(revision.content,submit['content']);self.assertEqual(len(revision.attachment_manifest),1);self.assertEqual(RevisionAttachment.objects.count(),1)


class ContentTests(SimpleTestCase):
    def test_document_image_types_and_malformed_content(self):
        for kind,raw in [('pdf',pdf_bytes()),('docx',docx_bytes()),('txt','Студенческий текст'.encode()),('jpeg',image_bytes('JPEG')),('png',image_bytes()),('webp',image_bytes('WEBP'))]:
            inspect(kind,raw)
        for kind,raw in [('pdf',b'%PDF-1.7\n%%EOF'),('docx',docx_bytes(True)),('txt',b'<svg onload="bad"/>'),('png',b'\x89PNG\r\n'),('txt',b'MZ\x00executable')]:
            with self.subTest(kind=kind),self.assertRaises(Exception):inspect(kind,raw)

    def test_xml_encoding_structure_archive_and_animation_rejection(self):
        from materials.file_inspector import safe_xml
        for encoding in ('utf-8','utf-16','utf-32'):
            with self.subTest(encoding=encoding),self.assertRaises(Exception):safe_xml(('<?xml version="1.0" encoding="'+encoding+'"?><!DOCTYPE x [<!ENTITY z "expanded">]><x>&z;</x>').encode(encoding))
        for filename,content in [('[Content_Types].xml','<wrong-root><wrong-entry ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></wrong-root>'),('_rels/.rels','<not-relationships/>')]:
            source=zipfile.ZipFile(io.BytesIO(docx_bytes()));out=io.BytesIO()
            with zipfile.ZipFile(out,'w') as target:
                for info in source.infolist():target.writestr(info.filename,content if info.filename==filename else source.read(info.filename))
            with self.assertRaises(Exception):inspect('docx',out.getvalue())
        from PIL import Image
        out=io.BytesIO();Image.new('RGB',(2,2),'red').save(out,format='WEBP',save_all=True,append_images=[Image.new('RGB',(2,2),'blue')],duration=100,loop=0)
        with self.assertRaises(Exception):inspect('webp',out.getvalue())
