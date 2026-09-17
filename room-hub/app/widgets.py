"""Trusted browser widgets only: no implicit server code execution."""
import json,re
from pathlib import Path

def discover(root):
 result=[];errors=[]
 for folder in sorted(Path(root).iterdir()):
  if not folder.is_dir() or folder.name.startswith(('_','.')):continue
  try:
   if folder.is_symlink() or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',folder.name):raise ValueError('올바르지 않은 폴더명')
   m=json.loads((folder/'manifest.json').read_text('utf-8'))
   if m.get('id')!=folder.name or m.get('apiVersion')!=1:raise ValueError('id 또는 apiVersion 불일치')
   for key,default,ext in [('entry','widget.js','js'),('style','style.css','css')]:
    filename=m.get(key,default)
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.'+ext,filename):raise ValueError('폴더 내부 엔트리 파일을 지정하세요.')
    path=folder/filename
    if not path.is_file() or path.is_symlink():raise ValueError(filename+' 파일이 없습니다.')
    m[key]=filename
   m['name']=str(m.get('name',folder.name))[:80];m['description']=str(m.get('description',''))[:500];m['baseUrl']='/widgets/'+folder.name
   result.append(m)
  except Exception as e:errors.append({'folder':folder.name,'error':str(e)})
 return result,errors
