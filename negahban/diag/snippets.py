"""Vetted, read-only Python health snippets that run INSIDE an app container (via exec.py).
Each check is a function c_<name>(A) -> dict using only the stdlib, reading /proc, cgroup files and the filesystem.
Nothing here writes, deletes, signals or opens network connections other than loopback (http_local).
compose() builds one program: helpers + the selected checks + a footer that prints a single marker line with JSON."""
import json, zlib

MARK = '@@NGH@@'

HEADER = r'''
import json,os,sys,time,re
T0=time.time()
def _r(p,n=262144):
    try:
        with open(p,'rb') as f: return f.read(n).decode('utf-8','replace')
    except Exception: return None
def _kv(t):
    d={}
    for l in (t or '').splitlines():
        p=l.replace(':',' ').split()
        if len(p)>=2:
            try: d[p[0]]=int(p[1])
            except ValueError: pass
    return d
def _mb(b): return round(b/1048576,1) if isinstance(b,(int,float)) else None
def _int(s):
    s=(s or '').strip()
    return int(s) if s.isdigit() else None
'''

CHECKS = {
'processes': r'''
def c_processes(A):
    out=[];tick=os.sysconf('SC_CLK_TCK');pg=os.sysconf('SC_PAGE_SIZE');me=os.getpid()
    up=float((_r('/proc/uptime') or '0').split()[0])
    for p in os.listdir('/proc'):
        if not p.isdigit() or int(p)==me: continue
        st=_r('/proc/%s/stat'%p)
        if not st: continue
        try:
            name=st[st.index('(')+1:st.rindex(')')];f=st[st.rindex(')')+2:].split()
            cmd=(_r('/proc/%s/cmdline'%p,4096) or '').replace('\0',' ').strip()[:300]
            try: nfd=len(os.listdir('/proc/%s/fd'%p))
            except Exception: nfd=None
            out.append({'pid':int(p),'ppid':int(f[1]),'state':f[0],'name':name,'cmd':cmd,'rss_mb':_mb(int(f[21])*pg),
                        'threads':int(f[17]),'cpu_s':round((int(f[11])+int(f[12]))/tick,1),'age_s':round(up-int(f[19])/tick),'fds':nfd})
        except Exception: pass
    out.sort(key=lambda x:-(x['rss_mb'] or 0))
    return {'count':len(out),'zombies':sum(1 for x in out if x['state']=='Z'),'processes':out[:int(A.get('limit',25))]}
''',
'memory': r'''
def c_memory(A):
    cg='/sys/fs/cgroup/';v2=_r(cg+'memory.current') is not None
    if v2: lim,cur,peak,st,ev=_int(_r(cg+'memory.max')),_int(_r(cg+'memory.current')),_int(_r(cg+'memory.peak')),_kv(_r(cg+'memory.stat')),_kv(_r(cg+'memory.events'))
    else: lim,cur,peak,st,ev=_int(_r(cg+'memory/memory.limit_in_bytes')),_int(_r(cg+'memory/memory.usage_in_bytes')),_int(_r(cg+'memory/memory.max_usage_in_bytes')),_kv(_r(cg+'memory/memory.stat')),{}
    if lim and lim>=1<<60: lim=None
    mi=_kv(_r('/proc/meminfo'))
    return {'cgroup':'v2' if v2 else 'v1','limit_mb':_mb(lim),'usage_mb':_mb(cur),'peak_mb':_mb(peak),'util':round(cur/lim,3) if cur and lim else None,
            'anon_mb':_mb(st.get('anon',st.get('rss'))),'file_cache_mb':_mb(st.get('file',st.get('cache'))),'kernel_mb':_mb(st.get('kernel',st.get('slab'))),
            'sock_mb':_mb(st.get('sock')),'oom_events':ev.get('oom'),'oom_kills':ev.get('oom_kill'),'limit_hits':ev.get('max'),
            'psi':(_r(cg+'memory.pressure') or _r('/proc/pressure/memory') or '').strip(),
            'host_total_mb':_mb(mi.get('MemTotal',0)*1024),'host_available_mb':_mb(mi.get('MemAvailable',0)*1024)}
''',
'cpu': r'''
def c_cpu(A):
    cg='/sys/fs/cgroup/';mx=(_r(cg+'cpu.max') or '').split();st=_kv(_r(cg+'cpu.stat'))
    q=round(int(mx[0])/int(mx[1]),2) if len(mx)==2 and mx[0].isdigit() else None
    u0=st.get('usage_usec');time.sleep(1);u1=_kv(_r(cg+'cpu.stat')).get('usage_usec')
    return {'limit_cores':q,'now_cores':round((u1-u0)/1e6,3) if u0 is not None and u1 is not None else None,'nr_periods':st.get('nr_periods'),
            'nr_throttled':st.get('nr_throttled'),'throttled_s':round(st.get('throttled_usec',0)/1e6,1),
            'throttle_ratio':round(st['nr_throttled']/st['nr_periods'],3) if st.get('nr_periods') else None,
            'psi':(_r(cg+'cpu.pressure') or _r('/proc/pressure/cpu') or '').strip(),'host_loadavg':(_r('/proc/loadavg') or '').strip(),'host_cpus':os.cpu_count()}
''',
'disk': r'''
def c_disk(A):
    skip=('proc','sysfs','cgroup','cgroup2','devpts','mqueue','securityfs','debugfs','tracefs','pstore','bpf','configfs','fusectl','autofs','nsfs','hugetlbfs')
    cand={'/':'?','/tmp':'?','/data':'?','/app':'?'}
    for l in (_r('/proc/mounts') or '').splitlines():
        p=l.split()
        if len(p)>=4 and p[2] not in skip and not p[1].startswith(('/proc','/sys','/dev','/etc/')): cand[p[1]]=p[2]+(' ro' if 'ro' in p[3].split(',') else '')
    out,seen=[],set()
    for m in sorted(cand):
        try: s=os.statvfs(m);d=os.stat(m).st_dev
        except Exception: continue
        if d in seen: continue
        seen.add(d);tot=s.f_blocks*s.f_frsize;free=s.f_bavail*s.f_frsize
        out.append({'mount':m,'fs':cand[m],'total_mb':_mb(tot),'used_mb':_mb(tot-s.f_bfree*s.f_frsize),'avail_mb':_mb(free),
                    'use_pct':round(100*(1-s.f_bavail/s.f_blocks),1) if s.f_blocks else None,'inodes_use_pct':round(100*(1-s.f_favail/s.f_files),1) if s.f_files else None})
    return {'mounts':out}
''',
'data_usage': r'''
def c_data_usage(A):
    root=A.get('path') or '/data';tops={};big=[];n=0;trunc=False
    if not os.path.isdir(root): return {'error':'not a directory: '+root}
    for dp,dn,fn in os.walk(root):
        if time.time()-T0>20 or n>200000: trunc=True;break
        rel=os.path.relpath(dp,root).split(os.sep)[0]
        for f in fn:
            n+=1;p=os.path.join(dp,f)
            try: s=os.lstat(p).st_size
            except Exception: continue
            k=f if rel=='.' else rel;tops[k]=tops.get(k,0)+s;big.append((s,p))
        if len(big)>2000: big=sorted(big,reverse=True)[:30]
    big=sorted(big,reverse=True)[:15]
    return {'path':root,'files':n,'total_mb':_mb(sum(tops.values())),'truncated':trunc,
            'top':[{'name':k,'mb':_mb(v)} for k,v in sorted(tops.items(),key=lambda x:-x[1])[:20]],'largest_files':[{'path':p,'mb':_mb(s)} for s,p in big]}
''',
'open_fds': r'''
def c_open_fds(A):
    out=[]
    for p in os.listdir('/proc'):
        if not p.isdigit() or int(p)==os.getpid(): continue
        kinds={};files={}
        try: fds=os.listdir('/proc/%s/fd'%p)
        except Exception: continue
        for fd in fds:
            try: t=os.readlink('/proc/%s/fd/%s'%(p,fd))
            except Exception: continue
            k=t.split(':')[0] if ':' in t and not t.startswith('/') else ('file' if t.startswith('/') else t)
            kinds[k]=kinds.get(k,0)+1
            if t.startswith('/') and not t.startswith('/dev/'): files[t]=files.get(t,0)+1
        lim=None
        for l in (_r('/proc/%s/limits'%p) or '').splitlines():
            if l.startswith('Max open files'): lim=l.split()[3]
        name=(_r('/proc/%s/comm'%p) or '').strip()
        out.append({'pid':int(p),'name':name,'fds':len(fds),'limit':int(lim) if lim and lim.isdigit() else lim,'by_type':kinds,
                    'top_files':sorted(files.items(),key=lambda x:-x[1])[:8]})
    out.sort(key=lambda x:-x['fds'])
    return {'processes':out[:15]}
''',
'ports': r'''
def c_ports(A):
    S={'01':'ESTABLISHED','02':'SYN_SENT','03':'SYN_RECV','04':'FIN_WAIT1','05':'FIN_WAIT2','06':'TIME_WAIT','07':'CLOSE','08':'CLOSE_WAIT','09':'LAST_ACK','0A':'LISTEN','0B':'CLOSING'}
    def ip(h):
        if len(h)==8: return '.'.join(str(int(h[i:i+2],16)) for i in (6,4,2,0))
        if h.startswith('0000000000000000FFFF0000'): return ip(h[24:])
        return 'v6:'+h[-8:]
    listen=[];states={};remote={}
    for f in ('/proc/net/tcp','/proc/net/tcp6'):
        for l in (_r(f) or '').splitlines()[1:]:
            p=l.split()
            if len(p)<4: continue
            la,lp=p[1].split(':');ra,rp=p[2].split(':');st=S.get(p[3],p[3]);states[st]=states.get(st,0)+1
            if st=='LISTEN': listen.append({'addr':ip(la),'port':int(lp,16),'proto':f[-4:].replace('/','')})
            elif st in ('ESTABLISHED','CLOSE_WAIT','SYN_SENT'):
                k='%s:%d %s'%(ip(ra),int(rp,16),st);remote[k]=remote.get(k,0)+1
    return {'listening':sorted(listen,key=lambda x:x['port']),'tcp_states':states,'top_remote':sorted(remote.items(),key=lambda x:-x[1])[:15]}
''',
'env_names': r'''
def c_env_names(A):
    names=sorted(os.environ);exp=[x for x in A.get('expect',[]) if x]
    return {'count':len(names),'names':names,'empty':[k for k in names if not os.environ[k].strip()],'missing_expected':[x for x in exp if x not in os.environ]}
''',
'sqlite_check': r'''
def c_sqlite_check(A):
    import sqlite3
    paths=[A['path']] if A.get('path') else []
    if not paths:
        for root in ('/data','/app'):
            for dp,dn,fn in os.walk(root):
                if dp.count(os.sep)-root.count(os.sep)>=3: dn[:]=[]
                paths+=[os.path.join(dp,f) for f in fn if f.endswith(('.db','.sqlite','.sqlite3'))]
    out=[]
    for p in paths[:10]:
        r={'path':p}
        try:
            r['size_mb']=_mb(os.path.getsize(p));r['wal_mb']=_mb(os.path.getsize(p+'-wal')) if os.path.exists(p+'-wal') else 0
            con=sqlite3.connect('file:%s?mode=ro'%p,uri=True,timeout=3);dl=time.time()+float(A.get('budget',20))
            con.set_progress_handler(lambda:1 if time.time()>dl else 0,20000)
            q=lambda s:con.execute(s).fetchall()
            r['journal_mode']=q('PRAGMA journal_mode')[0][0];r['page_size']=q('PRAGMA page_size')[0][0];r['pages']=q('PRAGMA page_count')[0][0]
            r['freelist_pages']=q('PRAGMA freelist_count')[0][0];r['tables']=q("SELECT count(*) FROM sqlite_master WHERE type='table'")[0][0]
            if A.get('skip_check'): r['check']='skipped';r['ok']=None
            else:
                try: r['check']=[x[0] for x in q('PRAGMA integrity_check(20)' if A.get('full') else 'PRAGMA quick_check(20)')];r['ok']=r['check']==['ok']
                except sqlite3.OperationalError as e: r['check']='interrupted (time budget)' if 'interrupt' in str(e) else str(e)[:200];r['ok']=None
            con.close()
        except Exception as e: r['error']=type(e).__name__+': '+str(e)[:200];r['ok']=False
        out.append(r)
    return {'databases':out,'found':len(paths)}
''',
'log_tracebacks': r'''
def c_log_tracebacks(A):
    dirs=A.get('dirs') or ['/data','/app','/tmp','/var/log'];files=[]
    for root in dirs:
        for dp,dn,fn in os.walk(root):
            if dp.count(os.sep)-root.count(os.sep)>=3: dn[:]=[]
            files+=[os.path.join(dp,f) for f in fn if re.search(r'\.(log|out|err)(\.\d)?$',f)]
    G={};errs=0;scanned=[]
    fr=re.compile(r'File "([^"]+)", line (\d+), in (\S+)');ex=re.compile(r'^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning))(?::\s*(.*))?$')
    for p in files[:40]:
        try:
            sz=os.path.getsize(p)
            with open(p,'rb') as f:
                f.seek(max(0,sz-2097152));lines=f.read().decode('utf-8','replace').splitlines()
        except Exception: continue
        scanned.append({'path':p,'mb':_mb(sz)});cur=None
        for l in lines:
            if re.search(r'\b(ERROR|CRITICAL|FATAL)\b',l): errs+=1
            if l.startswith('Traceback'): cur={'frames':[],'text':[]};continue
            if cur is None: continue
            cur['text'].append(l[:300]);m=fr.search(l)
            if m: cur['frames'].append((m.group(1),int(m.group(2)),m.group(3)));continue
            m=ex.match(l.strip())
            if m and cur['frames']:
                app=[x for x in cur['frames'] if 'site-packages' not in x[0] and '/lib/python' not in x[0]] or cur['frames'];t=app[-1]
                k='%s@%s:%d'%(m.group(1),t[0],t[1]);g=G.setdefault(k,{'signature':k,'exc':m.group(1),'msg':(m.group(2) or '')[:200],'count':0,'file':p,'sample':cur['text'][-12:]})
                g['count']+=1;cur=None
            elif len(cur['text'])>80: cur=None
    return {'files':scanned,'error_lines':errs,'groups':sorted(G.values(),key=lambda g:-g['count'])[:10]}
''',
'http_local': r'''
def c_http_local(A):
    import urllib.request,urllib.error
    port=A.get('port')
    if not port:
        c=(_r('/proc/1/cmdline') or '').split('\0')
        for i,x in enumerate(c):
            if x in ('--port','-p') and i+1<len(c) and c[i+1].isdigit(): port=int(c[i+1])
            m=re.search(r'(?:0\.0\.0\.0|\[::\]|:):(\d{2,5})$',x)
            if m and not port: port=int(m.group(1))
    port=port or int(os.environ.get('PORT') or 8080);url='http://127.0.0.1:%d%s'%(port,A.get('path') or '/')
    t=time.time()
    try:
        r=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'negahban-diag'}),timeout=float(A.get('timeout',8)));st=r.status;h=dict(r.headers);b=r.read(4000)
    except urllib.error.HTTPError as e: st=e.code;h=dict(e.headers or {});b=e.read(4000)
    except Exception as e: return {'url':url,'ok':False,'error':type(e).__name__+': '+str(e)[:200],'ms':round((time.time()-t)*1000)}
    return {'url':url,'status':st,'ok':st<500,'ms':round((time.time()-t)*1000),'headers':{k:v for k,v in h.items() if k.lower() in ('content-type','content-length','server','x-request-id','retry-after','location')},
            'body':b.decode('utf-8','replace')[:1500]}
''',
'runtime_info': r'''
def c_runtime_info(A):
    import platform
    try:
        from importlib import metadata;pk=sorted('%s==%s'%(d.metadata['Name'],d.version) for d in metadata.distributions())
    except Exception: pk=[]
    os_rel=dict(l.split('=',1) for l in (_r('/etc/os-release') or '').splitlines() if '=' in l).get('PRETTY_NAME','').strip('"')
    st=(_r('/proc/1/stat') or '').rsplit(')',1)[-1].split();up=float((_r('/proc/uptime') or '0').split()[0])
    return {'python':sys.version.split()[0],'executable':sys.executable,'os':os_rel,'uid':os.getuid(),'hostname':platform.node(),'cwd':os.getcwd(),
            'pid1_cmd':(_r('/proc/1/cmdline',2048) or '').replace('\0',' ').strip()[:300],'container_uptime_s':round(up-int(st[19])/os.sysconf('SC_CLK_TCK')) if len(st)>19 else None,
            'tz':os.environ.get('TZ') or time.tzname[0],'packages':pk[:300],'package_count':len(pk)}
''',
'limits': r'''
def c_limits(A):
    keep=('Max open files','Max processes','Max address space','Max stack size','Max file size','Max locked memory')
    lim={}
    for l in (_r('/proc/1/limits') or '').splitlines():
        for k in keep:
            if l.startswith(k): p=l[len(k):].split();lim[k]={'soft':p[0],'hard':p[1]}
    cg='/sys/fs/cgroup/'
    return {'pid1_limits':lim,'pids_current':_int(_r(cg+'pids.current')),'pids_max':(_r(cg+'pids.max') or '').strip() or None,
            'tmp_writable':os.access('/tmp',os.W_OK),'root_readonly':not os.access('/',os.W_OK)}
''',
}

OVERVIEW = ('memory', 'cpu', 'disk', 'processes')


def compose(checks, args=None):
    """Source of one program running `checks` with args A; prints MARK + json({check: result|{'error':..}})."""
    body = [HEADER, 'A=json.loads(bytes.fromhex(%r).decode())' % json.dumps(args or {}).encode().hex()]
    body += [CHECKS[c] for c in checks]
    body.append('R={}\nfor _n in %r:\n    try: R[_n]=globals()["c_"+_n](A)\n    except Exception as _e: R[_n]={"error":type(_e).__name__+": "+str(_e)[:200]}\n'
                'R["_ms"]=round((time.time()-T0)*1000)\nprint(%r+json.dumps(R,default=str))\n' % (list(checks), MARK))
    return '\n'.join(body)


def command(checks, args=None, python='python3'):
    """argv for the Liara exec API: no spaces and no '+' (the API splits on both), so the program travels zlib+hex encoded."""
    src = compose(checks, args)
    hexed = zlib.compress(src.encode(), 9).hex()
    return [python, '-c', f"exec(__import__('zlib').decompress(bytes.fromhex('{hexed}')))"]


def parse(output):
    """Extract the JSON result printed after MARK (the PTY may add \\r and wrap nothing else)."""
    i = output.rfind(MARK)
    if i < 0:
        return None
    line = output[i + len(MARK):].replace('\r', '').split('\n', 1)[0]
    try:
        return json.loads(line)
    except ValueError:
        return None
