"""PKI поверх openssl: CA OpenVPN (index.txt, CRL, tls-crypt) и свой CA IKEv2 (stdlib only).

Программы зовутся через `run=` (служба управления подставляет свой ограниченный запуск).
Проверки да/нет не бросают; остальное бросает PkiError с программой и последней строкой
stderr - без аргументов и входных данных, приватный ключ в текст не попадает.
"""
import contextlib
import ctypes
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from vpn2.fsutil import atomic, lock

HEX = re.compile(r'[0-9A-Fa-f]{1,64}')
BLANK = ' \t\n\v\f\r'
KEY_BEGIN = '-----BEGIN OpenVPN Static key V1-----'
KEY_END = '-----END OpenVPN Static key V1-----'
# openssl.cnf CA OpenVPN, каталог PKI - @DIR@.
CNF = '''[ca]
default_ca = CA_default

[CA_default]
dir              = @DIR@
database         = $dir/index.txt
new_certs_dir    = $dir/newcerts
certificate      = $dir/ca.crt
private_key      = $dir/ca.key
serial           = $dir/serial
crlnumber        = $dir/crlnumber
crl              = $dir/crl.pem
default_md       = sha256
default_days     = 3650
default_crl_days = 3650
policy           = policy_anything
unique_subject   = no
copy_extensions  = none

[policy_anything]
commonName             = supplied
countryName            = optional
stateOrProvinceName    = optional
localityName           = optional
organizationName       = optional
organizationalUnitName = optional
emailAddress           = optional

[req]
distinguished_name = req_dn
prompt             = no

[req_dn]
CN = anysda-vpn2

[v3_ca]
basicConstraints     = critical,CA:TRUE
keyUsage             = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash

[server_ext]
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature,keyEncipherment
extendedKeyUsage       = serverAuth
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer

[client_ext]
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature
extendedKeyUsage       = clientAuth
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
'''
EC_KEY = ['-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1']


class PkiError(Exception):
    """Отказ openssl или битая база CA; code - код возврата, если он был."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class PkiIdentity(PkiError):
    """Живой сертификат при битом или пропавшем ключе: заменять нельзя, только восстановить из бэкапа."""


class Row(NamedTuple):
    """Строка index.txt."""
    status: str
    expires: str
    revoked: str
    serial: str
    file: str
    subject: str


def run(argv, *, data=None, timeout=60):
    try:
        return subprocess.run(argv, input=data, capture_output=True, text=True, errors='replace',
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise PkiError(f'{argv[0]}: timeout') from None
    except OSError as error:
        raise PkiError(f'{argv[0]}: {error.strerror or error}') from None


def _checked(runner, argv, label, data=None):
    done = runner(argv) if data is None else runner(argv, data=data)
    if done.returncode:
        lines = (done.stderr or '').strip().splitlines()
        raise PkiError(f'{label}: {lines[-1] if lines else "?"}', done.returncode)
    return done


def _ok(runner, argv):
    try:
        return runner(argv).returncode == 0
    except PkiError:
        return False


def tls_crypt_valid(text):
    """Ключ tls-crypt: между маркерами V1 ровно 512 hex; строки с # и пустые пропускаются."""
    lines = [line for line in text.split('\n') if line.strip(BLANK) and not line.startswith('#')]
    body = lines[1:-1]
    return (len(lines) > 2 and lines[0] == KEY_BEGIN and lines[-1] == KEY_END
            and all(re.fullmatch('[0-9a-fA-F]+', line) for line in body) and sum(map(len, body)) == 512)


def index(text):
    """Строки index.txt как Row; всё, что не из шести колонок, пропускается."""
    rows = (line.split('\t') for line in text.splitlines())
    return [Row(*columns) for columns in rows if len(columns) == 6]


def by_cn(rows, cn, *, strict=False):
    """Строки с этим CN (dev_7 не находит dev_70); плохой serial: strict - PkiError, иначе пропуск."""
    found = re.compile(rf'(?:^|/)CN={re.escape(cn)}(?:/|$)')
    matches = []
    for row in rows:
        if found.search(row.subject):
            if HEX.fullmatch(row.serial):
                matches.append(row)
            elif strict:
                raise PkiError(f'index.txt: bad serial for CN={cn}')
    return matches


def openssl_cnf(directory='/etc/openvpn/server/pki'):
    return CNF.replace('@DIR@', str(directory))


def key_valid(path, *, run=run):
    return _ok(run, ['openssl', 'pkey', '-in', str(path), '-check', '-noout'])


def cert_valid(path, *, checkend=0, run=run):
    return _ok(run, ['openssl', 'x509', '-in', str(path), '-noout', '-checkend', str(checkend)])


def pair_valid(key, cert, *, run=run):
    """Ключ и сертификат валидны, публичный ключ у них один."""
    if not (key_valid(key, run=run) and cert_valid(cert, run=run)):
        return False
    try:
        derived = run(['openssl', 'pkey', '-in', str(key), '-pubout'])
        issued = run(['openssl', 'x509', '-in', str(cert), '-pubkey', '-noout'])
    except PkiError:
        return False
    return derived.returncode == 0 == issued.returncode and derived.stdout.strip() == issued.stdout.strip()


def genkey(path, *, run=run):
    _checked(run, ['openssl', 'genpkey', *EC_KEY, '-out', str(path)], 'openssl genpkey')


def require_key(key, cert, *, run=run):
    """Ключ годен или создан заново; битый ключ при живом сертификате - PkiIdentity."""
    if key_valid(key, run=run):
        return
    if cert_valid(cert, run=run):
        raise PkiIdentity(f'PKI: valid certificate but missing/invalid private key: {key}')
    genkey(key, run=run)


def verify(ca, cert, *, crl=None, run=run):
    argv = ['openssl', 'verify', '-CAfile', str(ca)]
    if crl is not None:
        argv += ['-CRLfile', str(crl), '-crl_check']
    return _ok(run, [*argv, str(cert)])


def issue(directory, cn, key, csr, out, *, run=run):
    """Клиентский сертификат для key через `openssl ca` (запись в index/serial CA); serial hex-строкой."""
    _checked(run, ['openssl', 'req', '-new', '-key', str(key), '-out', str(csr), '-subj', '/CN=' + cn],
             'openssl req')
    _checked(run, ['openssl', 'ca', '-batch', '-notext', '-config', str(Path(directory) / 'openssl.cnf'),
                   '-extensions', 'client_ext', '-days', '3650', '-in', str(csr), '-out', str(out)],
             'openssl ca')
    value = _checked(run, ['openssl', 'x509', '-in', str(out), '-noout', '-serial'],
                     'openssl x509').stdout.strip().partition('=')[2]
    if not HEX.fullmatch(value):
        raise PkiError('openssl x509 -serial: no serial')
    return value


def revoke(directory, cert, *, run=run):
    _checked(run, ['openssl', 'ca', '-batch', '-config', str(Path(directory) / 'openssl.cnf'),
                   '-revoke', str(cert)], 'openssl ca -revoke')


def gencrl(directory, out, *, run=run):
    """CRL из базы CA сначала во временный файл, проверка по ca.crt, затем атомарно в out (0644)."""
    directory, out = Path(directory), Path(out)
    with tempfile.TemporaryDirectory(dir=out.parent) as work:
        crl = Path(work) / 'crl.pem'
        _checked(run, ['openssl', 'ca', '-batch', '-config', str(directory / 'openssl.cnf'), '-gencrl',
                       '-out', str(crl)], 'openssl ca -gencrl')
        _checked(run, ['openssl', 'crl', '-in', str(crl), '-noout', '-verify', '-CAfile',
                       str(directory / 'ca.crt')], 'openssl crl')
        atomic(out, crl.read_text(), 0o644)


def _exchange(source, target):
    """renameat2(RENAME_EXCHANGE): два каталога меняются местами одним syscall."""
    at_fdcwd, rename_exchange = -100, 2
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(target), rename_exchange):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


@contextlib.contextmanager
def transaction(target, *, timeout=None):
    """Комплект target собирается в рабочей копии рядом и подменяет target целиком.

    Замок `.<имя>.lock` рядом с target делят стадия и панель. Внутри блока umask 077.
    Исключение - target не тронут; успех - права target на копию и обмен каталогов.
    """
    target = Path(target)
    umask = os.umask(0o077)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with lock(target.parent / f'.{target.name}.lock', timeout=timeout):
            work = Path(tempfile.mkdtemp(prefix=f'{target.name}.tmp.', dir=target.parent))
            try:
                if target.is_dir():
                    # Как `cp -a --no-preserve=mode`: права от umask 077, владельцы - если root.
                    shutil.copytree(target, work, symlinks=True, dirs_exist_ok=True)
                    for path in Path(work).rglob('*'):
                        info = (target / path.relative_to(work)).lstat()
                        if os.geteuid() == 0:
                            os.lchown(path, info.st_uid, info.st_gid)
                        if not path.is_symlink():
                            path.chmod(info.st_mode & 0o700)
                    if os.geteuid() == 0:
                        os.lchown(work, target.stat().st_uid, target.stat().st_gid)
                yield work
                if os.path.exists(target):
                    work.chmod(os.stat(target).st_mode & 0o777)
                    _exchange(work, target)
                else:
                    os.rename(work, target)
            finally:
                shutil.rmtree(work, ignore_errors=True)
    finally:
        os.umask(umask)


def _require_pair(key, cert, run):
    if not pair_valid(key, cert, run=run):
        raise PkiError(f'PKI: invalid or mismatched key/certificate: {cert}')


def _ensure_ca(ca_key, ca, create, run):
    """CA с годным ключом: create() выпускает его, когда сертификата нет или он негоден."""
    require_key(ca_key, ca, run=run)
    if not cert_valid(ca, run=run):
        create()
    _require_pair(ca_key, ca, run)
    _checked(run, ['openssl', 'verify', '-CAfile', str(ca), str(ca)], 'openssl verify')


def _check_server(ca, key, cert, run):
    _require_pair(key, cert, run)
    _checked(run, ['openssl', 'verify', '-CAfile', str(ca), '-purpose', 'sslserver', str(cert)], 'openssl verify')


def _chmod(private, public):
    for path in private:
        path.chmod(0o600)
    for path in public:
        path.chmod(0o644)


def build_openvpn(directory, *, run=run):
    """Комплект OpenVPN (CA, сервер, tls-crypt, CRL) в directory; готовый комплект не перевыпускается.

    openssl.cnf пишется с каталогом по умолчанию (туда комплект попадёт после transaction),
    `openssl ca` идёт по build.cnf с каталогом directory.
    """
    pki = Path(directory)
    (pki / 'newcerts').mkdir(parents=True, exist_ok=True)
    atomic(pki / 'openssl.cnf', openssl_cnf())
    for name, body in (('index.txt', ''), ('serial', '01\n'), ('crlnumber', '01\n')):
        if not (pki / name).is_file():
            atomic(pki / name, body)
    ca, ca_key, cnf, build = pki / 'ca.crt', pki / 'ca.key', pki / 'openssl.cnf', pki / 'build.cnf'
    _ensure_ca(ca_key, ca, lambda: _checked(
        run, ['openssl', 'req', '-x509', '-new', '-key', str(ca_key), '-days', '3650', '-sha256', '-out', str(ca),
              '-subj', '/CN=anysda-vpn2 CA', '-config', str(cnf), '-extensions', 'v3_ca'], 'openssl req'), run)
    atomic(build, openssl_cnf(pki))
    key, cert, csr = pki / 'server.key', pki / 'server.crt', pki / 'server.csr'
    require_key(key, cert, run=run)
    if not (pair_valid(key, cert, run=run) and verify(ca, cert, run=run)):
        _checked(run, ['openssl', 'req', '-new', '-key', str(key), '-out', str(csr),
                       '-subj', '/CN=anysda-vpn2-server', '-config', str(cnf)], 'openssl req')
        _checked(run, ['openssl', 'ca', '-batch', '-notext', '-config', str(build), '-extensions', 'server_ext',
                       '-days', '3650', '-in', str(csr), '-out', str(cert)], 'openssl ca')
        csr.unlink(missing_ok=True)
    tls, crl = pki / 'tls-crypt.key', pki / 'crl.pem'
    if not (tls.is_file() and tls_crypt_valid(tls.read_text(errors='replace'))):
        _checked(run, ['openvpn', '--genkey', 'secret', str(tls)], 'openvpn --genkey')
    crl_check = ['openssl', 'crl', '-in', str(crl), '-noout', '-verify', '-CAfile', str(ca)]
    if not _ok(run, crl_check):
        _checked(run, ['openssl', 'ca', '-config', str(build), '-gencrl', '-out', str(crl)], 'openssl ca -gencrl')
    _check_server(ca, key, cert, run)
    _checked(run, crl_check, 'openssl crl')
    if not (tls.is_file() and tls_crypt_valid(tls.read_text(errors='replace'))):
        raise PkiError(f'PKI: invalid tls-crypt key: {tls}')
    build.unlink(missing_ok=True)
    _chmod((ca_key, key, tls), (ca, cert, crl))


def build_ikev2(directory, public, entry_host, *, say=lambda _text: None, run=run):
    """Свой CA IKEv2 (10 лет, один раз) и сертификат сервера (5 лет) в directory.

    Сертификат перевыпускается, когда истёк, не сходится с CA или в SAN нет адреса профилей
    public либо IP входа; ключ и CA прежние, выданные профили продолжают работать.
    """
    pki = Path(directory)
    ca, ca_key, key, cert = pki / 'ca.crt', pki / 'ca.key', pki / 'server.key', pki / 'server.crt'

    def create_ca():
        say('generating CA (ECDSA P-256, 10y)')
        ca.write_text(_checked(run, ['pki', '--self', '--in', str(ca_key), '--type', 'ecdsa',
                                     '--dn', 'CN=anysda-vpn2 IKEv2 CA', '--ca', '--lifetime', '3650',
                                     '--outform', 'pem'], 'pki --self').stdout)

    _ensure_ca(ca_key, ca, create_ca, run)
    sans = [public] if public == entry_host else [public, entry_host]
    require_key(key, cert, run=run)
    reissue = not (pair_valid(key, cert, run=run) and verify(ca, cert, run=run))
    if not reissue:
        try:
            text = run(['openssl', 'x509', '-in', str(cert), '-noout', '-ext', 'subjectAltName']).stdout
        except PkiError:
            text = ''
        have = {part.lstrip(' ') for line in text.splitlines() for part in line.split(',')}
        for name in sans:
            if (f'IP Address:{name}' if re.fullmatch(r'[0-9.]+', name) else f'DNS:{name}') not in have:
                say(f'SAN не содержит {name}, перевыпуск server cert')
                reissue = True
    if reissue:
        pub = pki / 'server.pub'
        pub.write_text(_checked(run, ['pki', '--pub', '--in', str(key), '--type', 'ecdsa', '--outform', 'pem'],
                                'pki --pub').stdout)
        cert.write_text(_checked(run, ['pki', '--issue', '--cacert', str(ca), '--cakey', str(ca_key),
                                       '--in', str(pub), '--type', 'pub', '--dn', f'CN={public}',
                                       *(f'--san={name}' for name in sans),
                                       '--flag', 'serverAuth', '--flag', 'ikeIntermediate',
                                       '--lifetime', '1825', '--outform', 'pem'], 'pki --issue').stdout)
        pub.unlink()
    _check_server(ca, key, cert, run)
    _chmod((ca_key, key), (ca, cert))
