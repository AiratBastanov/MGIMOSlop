"""Собрать H.264 MP4 из законченных записей без ускорения и выдуманной аудиодорожки."""
import argparse,hashlib,json,os,subprocess,textwrap
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--capture',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
out=args.output.resolve();capture=json.loads(args.capture.read_text(encoding='utf-8'))
scenes=sorted(capture['scenes'],key=lambda s:s['number'])
assert [s['number'] for s in scenes]==list(range(1,9)),'Нужны все восемь подтверждённых сцен'
def run(cmd,label):
    p=subprocess.run(cmd,cwd=out,capture_output=True)
    (out/(label+'.log')).write_bytes(p.stdout+p.stderr)
    assert p.returncode==0,f'{label}: см. журнал'
def probe(path):
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]))
def timestamp(seconds,ass=False):
    units=100 if ass else 1000;n=round(seconds*units);h,n=divmod(n,3600*units);m,n=divmod(n,60*units);s,f=divmod(n,units)
    return f'{h}:{m:02}:{s:02}.{f:02}' if ass else f'{h:02}:{m:02}:{s:02},{f:03}'
header='''[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,32,&H00FFFFFF,&H00FFFFFF,&H00152338,&H00152338,0,0,0,0,100,100,0,0,1,0,0,2,80,80,12,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
total=0;cues=[];media=[]
for scene in scenes:
    number=scene['number'];duration=scene['duration'];raw=Path(scene['raw']);metadata=probe(raw)
    raw_duration=float(metadata['format']['duration']);start=max(0,raw_duration-duration)
    assert raw_duration>=duration-.5
    ass=header
    for i,cue in enumerate(scene['cues']):
        end=scene['cues'][i+1]['at'] if i+1<len(scene['cues']) else duration
        assert end>cue['at']
        value='\\N'.join(textwrap.wrap(cue['text'],width=100,break_long_words=False))
        assert len(value.split('\\N'))<=2
        ass+=f"Dialogue: 0,{timestamp(cue['at'],True)},{timestamp(end,True)},Default,,0,0,0,,{value}\n"
        cues.append({'start':round(total+cue['at'],3),'end':round(total+end,3),'text':cue['text'],'scene':number})
    (out/f'scene-{number:02}.ass').write_text(ass,encoding='utf-8')
    placement='(ow-iw)/2:64' if scene['mobile'] else '0:0'
    filters=f"fps=25,setsar=1,pad=1920:1080:{placement}:color=0x152338,ass=scene-{number:02}.ass"
    target=out/f'scene-{number:02}.mp4'
    run(['ffmpeg','-hide_banner','-y','-ss',str(start),'-i',str(raw),'-t',str(duration),'-vf',filters,'-an','-c:v','libx264','-preset','medium','-crf','20','-pix_fmt','yuv420p','-map_metadata','-1','-movflags','+faststart',str(target)],f'encode-{number:02}')
    measured=probe(target);actual=float(measured['format']['duration'])
    # Кадровое округление не должно накладывать последнюю подпись на следующую сцену.
    for cue in cues:
        if cue['scene']==number:
            cue['end']=min(cue['end'],round(total+actual,3))
    media.append({'scene':number,'raw_size':scene['viewport'],'trim_start':start,'duration':actual,'speed':1})
    total+=actual;print(f'Собрана сцена {number}: {actual:.2f} с',flush=True)
(out/'concat.txt').write_text(''.join(f"file 'scene-{s['number']:02}.mp4'\n" for s in scenes),encoding='ascii')
video=out/'digital-deanery.mp4'
run(['ffmpeg','-hide_banner','-y','-f','concat','-safe','0','-i','concat.txt','-c','copy','-an','-map_metadata','-1','-movflags','+faststart',str(video)],'concat')
info=probe(video);streams=info['streams'];v=next(s for s in streams if s['codec_type']=='video')
assert v['codec_name']=='h264' and v['pix_fmt']=='yuv420p' and (v['width'],v['height'])==(1920,1080)
assert not any(s['codec_type']=='audio' for s in streams)
assert 240<=float(info['format']['duration'])<=300
run(['ffmpeg','-hide_banner','-v','error','-i',str(video),'-f','null',os.devnull],'full-decode')
assert not (out/'full-decode.log').read_bytes(),'Ошибка декодирования'
run(['ffmpeg','-hide_banner','-y','-ss','4','-i',str(video),'-frames:v','1','-q:v','2','poster.jpg'],'poster')
(out/'demo.ru.srt').write_text('\n'.join(f"{i+1}\n{timestamp(c['start'])} --> {timestamp(c['end'])}\n{c['text']}\n" for i,c in enumerate(cues)),encoding='utf-8')
(out/'transcript.ru.md').write_text('# Демонстрация «Цифрового деканата»\n\nНастоящее приложение на отдельной синтетической базе. Все сведения вымышлены. Запись экрана без звуковой дорожки; русские подписи включены в изображение и доступны отдельным SRT.\n\n'+ '\n\n'.join(f"**{int(c['start'])//60}:{int(c['start'])%60:02}** {c['text']}" for c in cues)+'\n',encoding='utf-8')
data=video.read_bytes();offset=0;atoms=[]
while offset+8<=len(data):
    size=int.from_bytes(data[offset:offset+4],'big');name=data[offset+4:offset+8].decode('ascii');atoms.append(name)
    if size==1:size=int.from_bytes(data[offset+8:offset+16],'big')
    if size==0:break
    assert size>=8;offset+=size
assert atoms.index('moov')<atoms.index('mdat')
result={'build':capture['build'],'duration_seconds':float(info['format']['duration']),'width':v['width'],'height':v['height'],'codec':v['codec_name'],'pixel_format':v['pix_fmt'],'frame_rate':v['avg_frame_rate'],'audio':False,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'fast_start':True,'full_decode':'PASS','scenes':media,'captions':cues,'review':'PENDING'}
(out/'media.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:result[k] for k in ['build','duration_seconds','width','height','bytes','sha256','full_decode','review']},ensure_ascii=False),flush=True)
