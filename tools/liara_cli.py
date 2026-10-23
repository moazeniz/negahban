#!/usr/bin/env python3
"""liara_cli — tiny operator helper for the Liara REST API (stdlib only).

Credentials come from the environment (LIARA_API_TOKEN, LIARA_TEAM_ID, optional LIARA_API_BASE);
missing values are read from the env file named by LIARA_ENV_FILE (default ~/.liara.env, mode 600).
Secrets are never printed.

usage:
  liara_cli.py get PATH                      # e.g. get /v1/projects/negahban-sre
  liara_cli.py post PATH [JSON]
  liara_cli.py delete PATH
  liara_cli.py deploy APP SRCDIR PORT ENVJSON|ENVFILE.json [DISKSJSON]
  liara_cli.py wait APP                      # poll the newest release until a terminal state
  liara_cli.py releases APP [COUNT]          # id, state, type, created (newest first)
  liara_cli.py rollback RELEASE_ID           # POST /v1/releases/{id}/rollback (console endpoint)

NOTE: a release's `envs` REPLACES all app env vars on Liara — always pass the complete set."""
import io,json,os,sys,tarfile,time,urllib.request,urllib.error,uuid

def load_env():
    env={}
    path=os.path.expanduser(os.getenv('LIARA_ENV_FILE','~/.liara.env'))
    if os.path.exists(path):
        for line in open(path):
            line=line.strip()
            if '=' in line and not line.startswith('#'):k,v=line.split('=',1);env[k.strip()]=v.strip().strip('"\'')
    for k in('LIARA_API_TOKEN','LIARA_TEAM_ID','LIARA_API_BASE'):
        if os.getenv(k):env[k]=os.environ[k]
    missing=[k for k in('LIARA_API_TOKEN','LIARA_TEAM_ID') if not env.get(k)]
    if missing:sys.exit(f'missing {", ".join(missing)} (set env vars or LIARA_ENV_FILE)')
    return env.get('LIARA_API_BASE','https://api.iran.liara.ir').rstrip('/'),env['LIARA_TEAM_ID'],env['LIARA_API_TOKEN']

BASE=TEAM=TOK=None
def call(method,path,body=None,raw=None,ctype=None):
    url=BASE+path+('&' if '?' in path else '?')+'teamID='+TEAM
    data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req=urllib.request.Request(url,data=data,method=method,headers={'Authorization':'Bearer '+TOK,**({'Content-Type':ctype or 'application/json'} if data is not None else {})})
    try:
        with urllib.request.urlopen(req,timeout=300) as r:t=r.read();code=r.status
    except urllib.error.HTTPError as e:t=e.read();code=e.code
    try:return code,json.loads(t or b'null')
    except ValueError:return code,t.decode(errors='replace')[:2000]

SKIP_DIRS={'.git','node_modules','.venv','__pycache__','.pytest_cache','data','dist','tests','_private','private','secrets'}
def tarball(src):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tf:
        for root,dirs,files in os.walk(src):
            dirs[:]=[d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith(('.db','.pyc','.log','.zip','.private.md')) or f.startswith('.env'):continue
                p=os.path.join(root,f);tf.add(p,arcname=os.path.relpath(p,src))
    return buf.getvalue()

def deploy(app,src,port,envs,disks=None,message='deploy'):
    data=tarball(src);b='----x'+uuid.uuid4().hex
    body=(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="source.tar.gz"\r\nContent-Type: application/gzip\r\n\r\n').encode()+data+f'\r\n--{b}--\r\n'.encode()
    code,r=call('POST',f'/v2/projects/{app}/sources',raw=body,ctype='multipart/form-data; boundary='+b)
    print('upload',code,len(data),r if code>=300 else '')
    if code>=300:sys.exit(1)
    rel={'sourceID':r['sourceID'],'port':int(port),'type':'docker','envs':envs,'message':message}
    if disks:rel['disks']=disks
    code,r=call('POST',f'/v2/projects/{app}/releases',rel);print('release',code,r);return r

def wait(app,timeout=900):
    t0=time.time()
    while time.time()-t0<timeout:
        code,r=call('GET',f'/v1/projects/{app}/releases?page=1&count=1')
        st=r['releases'][0]['state'] if code==200 and r.get('releases') else code
        print(int(time.time()-t0),'s',st,flush=True)
        if st in('READY','FAILED','CANCELED','TIMEDOUT','UNHEALTHY'):return st
        time.sleep(10)
    return 'WAIT_TIMEOUT'

def releases(app,count=10):
    code,r=call('GET',f'/v1/projects/{app}/releases?page=1&count={int(count)}')
    if code!=200:print(code,r);return
    for x in r.get('releases',[]):print(x.get('_id'),x.get('state'),x.get('type'),x.get('createdAt'),(x.get('message') or '')[:60])

def main(a):
    global BASE,TEAM,TOK
    if not a or a[0] in('-h','--help','help'):print(__doc__);return
    BASE,TEAM,TOK=load_env()
    if a[0]=='get':c,r=call('GET',a[1]);print(c);print(json.dumps(r,ensure_ascii=False,indent=1)[:int(os.getenv('LIM','6000'))])
    elif a[0]=='post':c,r=call('POST',a[1],json.loads(a[2]) if len(a)>2 else None);print(c,json.dumps(r,ensure_ascii=False)[:3000])
    elif a[0]=='delete':c,r=call('DELETE',a[1]);print(c,r)
    elif a[0]=='deploy':deploy(a[1],a[2],a[3],json.loads(open(a[4]).read()) if os.path.exists(a[4]) else json.loads(a[4]),json.loads(a[5]) if len(a)>5 else None)
    elif a[0]=='wait':print(wait(a[1]))
    elif a[0]=='releases':releases(a[1],a[2] if len(a)>2 else 10)
    elif a[0]=='rollback':c,r=call('POST',f'/v1/releases/{a[1]}/rollback');print(c,r)
    else:sys.exit(__doc__)

if __name__=='__main__':main(sys.argv[1:])
