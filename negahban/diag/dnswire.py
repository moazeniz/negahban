"""Minimal RFC 1035 message codec for DNS-over-HTTPS (RFC 8484) queries: build a query, parse answers (with name compression)."""
import ipaddress, random, struct

TYPES = {'A': 1, 'NS': 2, 'CNAME': 5, 'SOA': 6, 'MX': 15, 'TXT': 16, 'AAAA': 28, 'SRV': 33, 'CAA': 257}
NAMES = {v: k for k, v in TYPES.items()}
RCODES = {0: 'NOERROR', 1: 'FORMERR', 2: 'SERVFAIL', 3: 'NXDOMAIN', 4: 'NOTIMP', 5: 'REFUSED'}


def build_query(name, rtype, qid=None):
    qid = random.randint(0, 65535) if qid is None else qid
    hdr = struct.pack('>HHHHHH', qid, 0x0100, 1, 0, 0, 0)  # RD=1
    q = b''
    for label in name.rstrip('.').split('.'):
        lb = label.encode('idna')
        if not 0 < len(lb) < 64:
            raise ValueError('invalid DNS name')
        q += bytes([len(lb)]) + lb
    return hdr + q + b'\x00' + struct.pack('>HH', TYPES[rtype], 1)


def _name(buf, off, depth=0):
    labels, jumped, end = [], False, off
    while True:
        if depth > 20 or off >= len(buf):
            raise ValueError('bad name')
        n = buf[off]
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack('>H', buf[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped, off, depth = True, ptr, depth + 1
            continue
        if n == 0:
            if not jumped:
                end = off + 1
            return '.'.join(labels) + '.', end
        labels.append(buf[off + 1:off + 1 + n].decode('ascii', 'replace'))
        off += 1 + n


def parse_response(buf):
    qid, flags, qd, an, ns, ar = struct.unpack('>HHHHHH', buf[:12])
    off = 12
    for _ in range(qd):
        _, off = _name(buf, off)
        off += 4
    answers = []
    for _ in range(an):
        name, off = _name(buf, off)
        rtype, rclass, ttl, rdlen = struct.unpack('>HHIH', buf[off:off + 10])
        off += 10
        rd = buf[off:off + rdlen]
        answers.append({'name': name, 'type': NAMES.get(rtype, str(rtype)), 'ttl': ttl, 'data': _rdata(buf, off, rtype, rd)})
        off += rdlen
    return {'id': qid, 'rcode': RCODES.get(flags & 0xF, str(flags & 0xF)), 'truncated': bool(flags & 0x0200), 'answers': answers}


def _rdata(buf, off, rtype, rd):
    if rtype == 1 and len(rd) == 4:
        return str(ipaddress.IPv4Address(rd))
    if rtype == 28 and len(rd) == 16:
        return str(ipaddress.IPv6Address(rd))
    if rtype in (2, 5):
        return _name(buf, off)[0]
    if rtype == 15:
        return f'{struct.unpack(">H", rd[:2])[0]} {_name(buf, off + 2)[0]}'
    if rtype == 16:
        out, i = [], 0
        while i < len(rd):
            n = rd[i]
            out.append(rd[i + 1:i + 1 + n].decode('utf-8', 'replace'))
            i += 1 + n
        return ''.join(out)
    if rtype == 257 and len(rd) >= 2:
        tl = rd[1]
        return f'{rd[0]} {rd[2:2 + tl].decode("ascii", "replace")} "{rd[2 + tl:].decode("utf-8", "replace")}"'
    if rtype == 6:
        mname, o = _name(buf, off)
        rname, o = _name(buf, o)
        return f'{mname} {rname} serial={struct.unpack(">I", buf[o:o + 4])[0]}'
    return rd.hex()
