"""PKI: openssl, index.txt, tls-crypt и openssl.cnf для OpenVPN (stdlib only).

Внешняя программа вызывается только через `run=`: тесты подменяют её.
Проверки, которые отвечают да/нет (`key_valid`, `cert_valid`, `pair_valid`,
`verify`), не бросают: любой отказ openssl для них - False. Остальные при отказе
бросают PkiError; в тексте только программа и последняя строка stderr - ни
аргументов, ни входных данных, значит приватный ключ в исключение не попадает.
"""
import contextlib
import ctypes
import os
import re
import secrets
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
# openssl.cnf: heredoc стадии 29, каталог PKI - @DIR@.
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
    """Живой сертификат при битом или пропавшем ключе: заменять нельзя."""


class Row(NamedTuple):
    """Строка index.txt: шесть колонок через табуляцию."""
    status: str
    expires: str
    revoked: str
    serial: str
    file: str
    subject: str

    @property
    def reason(self):
        """Причина отзыва: то, что в колонке отзыва после запятой."""
        return self.revoked.partition(',')[2]


def run(argv, *, data=None, timeout=60):
    """subprocess.run в текстовом режиме; нет программы или таймаут - PkiError."""
    try:
        return subprocess.run(argv, input=data, capture_output=True, text=True, errors='replace',
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise PkiError(f'{argv[0]}: timeout') from None
    except OSError as error:
        raise PkiError(f'{argv[0]}: {error.strerror or error}') from None


def _call(runner, argv, data=None):
    return runner(argv) if data is None else runner(argv, data=data)


def _last_line(done):
    lines = (done.stderr or '').strip().splitlines()
    return lines[-1] if lines else '?'


def _checked(runner, argv, label, data=None):
    done = _call(runner, argv, data)
    if done.returncode:
        raise PkiError(f'{label}: {_last_line(done)}', done.returncode)
    return done


def _ok(runner, argv):
    try:
        return _call(runner, argv).returncode == 0
    except PkiError:
        return False


def tls_crypt_valid(text):
    """Ключ tls-crypt (`openvpn --genkey secret`): между маркерами V1 ровно 512 hex.

    Строки с `#` и пустые пропускаются; любая другая строка вне формата - отказ.
    Устаревший static-key режим (--secret) не проверяется.
    """
    state, size = 0, 0
    for line in text.split('\n'):
        if line.startswith('#') or not line.strip(BLANK):
            continue
        if line == KEY_BEGIN and state == 0:
            state = 1
        elif line == KEY_END and state == 1:
            state = 2
        elif state == 1 and re.fullmatch('[0-9a-fA-F]+', line):
            size += len(line)
        else:
            return False
    return state == 2 and size == 512


def index(text):
    """Строки index.txt как Row; всё, что не из шести колонок, пропускается."""
    rows = (line.split('\t') for line in text.splitlines())
    return [Row(*columns) for columns in rows if len(columns) == 6]


def by_cn(rows, cn, *, comma=False, strict=False):
    """Строки с этим CN (dev_7 не находит dev_70).

    CN ограничен `/`; comma=True - ещё и `,` (так restore читает RFC2253-вид).
    Плохой hex в serial: strict - PkiError, иначе строка пропускается.
    """
    around = '[/,]' if comma else '/'
    found = re.compile(rf'(?:^|{around})CN={re.escape(cn)}(?:{around}|$)')
    matches = []
    for row in rows:
        if not found.search(row.subject):
            continue
        if not HEX.fullmatch(row.serial):
            if strict:
                raise PkiError(f'index.txt: bad serial for CN={cn}')
            continue
        matches.append(row)
    return matches


def openssl_cnf(directory='/etc/openvpn/server/pki'):
    """openssl.cnf для `openssl ca` с каталогом PKI directory."""
    return CNF.replace('@DIR@', str(directory))


def key_valid(path, *, run=run):
    """Приватный ключ читается и проходит `openssl pkey -check`."""
    return _ok(run, ['openssl', 'pkey', '-in', str(path), '-check', '-noout'])


def cert_valid(path, *, checkend=0, run=run):
    """Сертификат читается и не истекает в ближайшие checkend секунд."""
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
    """Новый приватный ключ EC prime256v1 в path."""
    _checked(run, ['openssl', 'genpkey', *EC_KEY, '-out', str(path)], 'openssl genpkey')


def require_key(key, cert, *, run=run):
    """Ключ годен или создан заново. Битый ключ при живом сертификате - PkiIdentity.

    Это может быть действующая идентичность, её восстанавливают из бэкапа, а не заменяют.
    """
    if key_valid(key, run=run):
        return
    if cert_valid(cert, run=run):
        raise PkiIdentity(f'PKI: valid certificate but missing/invalid private key: {key}')
    genkey(key, run=run)


def verify(ca, cert, *, crl=None, purpose=None, run=run):
    """`openssl verify`: сертификат подписан ca; с crl - и не отозван; purpose - как -purpose."""
    argv = ['openssl', 'verify', '-CAfile', str(ca)]
    if crl is not None:
        argv += ['-CRLfile', str(crl), '-crl_check']
    if purpose:
        argv += ['-purpose', purpose]
    return _ok(run, [*argv, str(cert)])


def serial(pem, *, run=run):
    """Серийник сертификата (PEM-текст) числом; None, если openssl его не отдал."""
    done = run(['openssl', 'x509', '-noout', '-serial'], data=pem)
    value = done.stdout.strip().partition('=')[2]
    return int(value, 16) if done.returncode == 0 and HEX.fullmatch(value) else None


def subject_cn(pem, *, run=run):
    """Первый CN из subject в виде RFC2253 (не-ASCII и спецзнаки экранированы); нет CN - None."""
    done = run(['openssl', 'x509', '-noout', '-subject', '-nameopt', 'RFC2253'], data=pem)
    found = re.search(r'(?:^|,)CN=((?:[^,\\]|\\.)*)', done.stdout.strip().partition('=')[2])
    return found[1] if done.returncode == 0 and found else None


def issue(directory, cn, key, csr, out, *, days=3650, extensions='client_ext', run=run):
    """Сертификат для key через `openssl ca` (запись в index/serial CA); возвращает serial hex-строкой."""
    directory = Path(directory)
    _checked(run, ['openssl', 'req', '-new', '-key', str(key), '-out', str(csr), '-subj', '/CN=' + cn],
             'openssl req')
    _checked(run, ['openssl', 'ca', '-batch', '-notext', '-config', str(directory / 'openssl.cnf'),
                   '-extensions', extensions, '-days', str(days), '-in', str(csr), '-out', str(out)],
             'openssl ca')
    done = _checked(run, ['openssl', 'x509', '-in', str(out), '-noout', '-serial'], 'openssl x509')
    value = done.stdout.strip().partition('=')[2]
    if not HEX.fullmatch(value):
        raise PkiError('openssl x509 -serial: no serial')
    return value


def sign_ephemeral(directory, cn, key, out, *, days=1, run=run):
    """Короткоживущий клиентский сертификат без `openssl ca`: index, serial и CRL не трогаются.

    Ключ создаётся в key, сертификат - в out, подписан ca.crt и ca.key из directory.
    """
    directory = Path(directory)
    with tempfile.TemporaryDirectory(dir=Path(out).parent) as work:
        csr, ext = Path(work) / 'csr.pem', Path(work) / 'ext.cnf'
        _checked(run, ['openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                       '-nodes', '-subj', '/CN=' + cn, '-keyout', str(key), '-out', str(csr)], 'openssl req')
        atomic(ext, 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n')
        _checked(run, ['openssl', 'x509', '-req', '-in', str(csr), '-CA', str(directory / 'ca.crt'),
                       '-CAkey', str(directory / 'ca.key'), '-set_serial', '0x' + secrets.token_hex(16),
                       '-days', str(days), '-extfile', str(ext), '-out', str(out)], 'openssl x509')


def revoke(directory, cert, *, reason=None, run=run):
    """Отметить сертификат отозванным в базе CA (CRL выпускает gencrl)."""
    _checked(run, ['openssl', 'ca', '-batch', '-config', str(Path(directory) / 'openssl.cnf'),
                   '-revoke', str(cert), *(['-crl_reason', reason] if reason else [])], 'openssl ca -revoke')


def gencrl(directory, out, *, verify=True, run=run):
    """CRL из базы CA в out.

    verify=True: выпуск во временный файл, проверка ca.crt и только потом атомарная
    запись 0644; не прошло проверку - PkiError, out прежний. verify=False: сразу в out.
    """
    directory, out = Path(directory), Path(out)
    argv = ['openssl', 'ca', '-batch', '-config', str(directory / 'openssl.cnf'), '-gencrl', '-out']
    if not verify:
        _checked(run, [*argv, str(out)], 'openssl ca -gencrl')
        return
    with tempfile.TemporaryDirectory(dir=out.parent) as work:
        crl = Path(work) / 'crl.pem'
        _checked(run, [*argv, str(crl)], 'openssl ca -gencrl')
        _checked(run, ['openssl', 'crl', '-in', str(crl), '-noout', '-verify', '-CAfile',
                       str(directory / 'ca.crt')], 'openssl crl')
        atomic(out, crl.read_text(), 0o644)


AT_FDCWD = -100
RENAME_EXCHANGE = 2


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _exchange(source, target):
    """renameat2(RENAME_EXCHANGE): два каталога меняются местами одним syscall."""
    if _libc().renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(target), RENAME_EXCHANGE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _copy(source, work):
    """Как `cp -a --no-preserve=mode source/. work/`: права от umask 077, владельцы - если root."""
    shutil.copytree(source, work, symlinks=True, dirs_exist_ok=True)
    for path in [work, *Path(work).rglob('*')]:
        origin = Path(source) / path.relative_to(work)
        info = origin.lstat()
        if os.geteuid() == 0:
            os.lchown(path, info.st_uid, info.st_gid)
        if path != Path(work) and not path.is_symlink():
            path.chmod(info.st_mode & 0o700)


@contextlib.contextmanager
def transaction(target, *, timeout=None):
    """Комплект target собирается в рабочей копии и подменяет target целиком.

    Замок `.<имя>.lock` лежит рядом с target (его делят стадия и панель), timeout - как
    у fsutil.lock. Внутри блока umask 077 (на весь процесс). Рабочая копия -
    `<target>.tmp.XXXXXXXX` рядом с target. Исключение в блоке: target не тронут, копия
    убрана. Успех: права target на копию и renameat2(EXCHANGE), запасного пути нет -
    отказ syscall это OSError. target ещё нет - простой rename. Остатки чужих упавших
    запусков (`*.tmp.*`) не трогаются: имя копии случайное, они не мешают.
    """
    target = Path(target)
    umask = os.umask(0o077)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with lock(target.parent / f'.{target.name}.lock', timeout=timeout):
            work = Path(tempfile.mkdtemp(prefix=f'{target.name}.tmp.', dir=target.parent))
            try:
                if target.is_dir():
                    _copy(target, work)
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


def build_openvpn(directory, *, run=run):
    """Комплект OpenVPN (CA, сервер, tls-crypt, CRL) в directory, как стадия 29.

    Повтор на готовом комплекте ничего не перевыпускает. openssl.cnf пишется с
    каталогом по умолчанию (туда комплект попадёт после transaction), а `openssl ca`
    идёт по build.cnf с каталогом directory, чтобы менялась только эта копия базы.
    """
    pki = Path(directory)
    (pki / 'newcerts').mkdir(parents=True, exist_ok=True)  # стадия 29:50, до транзакции
    atomic(pki / 'openssl.cnf', openssl_cnf())
    for name, body in (('index.txt', ''), ('serial', '01\n'), ('crlnumber', '01\n')):
        if not (pki / name).is_file():
            atomic(pki / name, body)
    ca, ca_key, cnf, build = pki / 'ca.crt', pki / 'ca.key', pki / 'openssl.cnf', pki / 'build.cnf'
    require_key(ca_key, ca, run=run)
    if not cert_valid(ca, run=run):
        _checked(run, ['openssl', 'req', '-x509', '-new', '-key', str(ca_key), '-days', '3650', '-sha256',
                       '-out', str(ca), '-subj', '/CN=anysda-vpn2 CA', '-config', str(cnf),
                       '-extensions', 'v3_ca'], 'openssl req')
    _require_pair(ca_key, ca, run)
    _checked(run, ['openssl', 'verify', '-CAfile', str(ca), str(ca)], 'openssl verify')
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

    def tls_ok():
        return tls.is_file() and tls_crypt_valid(tls.read_text(errors='replace'))

    if not tls_ok():
        _checked(run, ['openvpn', '--genkey', 'secret', str(tls)], 'openvpn --genkey')
    crl_check = ['openssl', 'crl', '-in', str(crl), '-noout', '-verify', '-CAfile', str(ca)]
    if not _ok(run, crl_check):
        _checked(run, ['openssl', 'ca', '-config', str(build), '-gencrl', '-out', str(crl)], 'openssl ca -gencrl')
    _require_pair(key, cert, run)
    _checked(run, ['openssl', 'verify', '-CAfile', str(ca), '-purpose', 'sslserver', str(cert)], 'openssl verify')
    _checked(run, crl_check, 'openssl crl')
    if not tls_ok():
        raise PkiError(f'PKI: invalid tls-crypt key: {tls}')
    build.unlink(missing_ok=True)
    for path in (ca_key, key, tls):
        path.chmod(0o600)
    for path in (ca, cert, crl):
        path.chmod(0o644)


def _san_names(cert, run):
    try:
        done = _call(run, ['openssl', 'x509', '-in', str(cert), '-noout', '-ext', 'subjectAltName'])
    except PkiError:
        return set()
    return {part.lstrip(' ') for line in done.stdout.splitlines() for part in line.split(',')}


def san_entry(name):
    """Запись SAN, как её печатает openssl: адрес из цифр и точек - IP, иначе DNS."""
    return f'IP Address:{name}' if re.fullmatch(r'[0-9.]+', name) else f'DNS:{name}'


def build_ikev2(directory, public, entry_host, *, say=lambda _text: None, run=run):
    """Свой CA IKEv2 и сертификат сервера в directory, как стадия 27.

    CA выпускается один раз на 10 лет. Сертификат сервера (5 лет) перевыпускается, когда
    истёк, не сходится с CA или в SAN нет адреса профилей public либо IP входа; ключ и
    CA при этом прежние, выданные профили продолжают работать.
    """
    pki = Path(directory)
    ca, ca_key = pki / 'ca.crt', pki / 'ca.key'
    require_key(ca_key, ca, run=run)
    if not cert_valid(ca, run=run):
        say('generating CA (ECDSA P-256, 10y)')
        done = _checked(run, ['pki', '--self', '--in', str(ca_key), '--type', 'ecdsa',
                              '--dn', 'CN=anysda-vpn2 IKEv2 CA', '--ca', '--lifetime', '3650',
                              '--outform', 'pem'], 'pki --self')
        ca.write_text(done.stdout)
    ca_key.chmod(0o600)
    ca.chmod(0o644)
    _require_pair(ca_key, ca, run)
    _checked(run, ['openssl', 'verify', '-CAfile', str(ca), str(ca)], 'openssl verify')

    sans = [public] if public == entry_host else [public, entry_host]
    key, cert = pki / 'server.key', pki / 'server.crt'
    require_key(key, cert, run=run)
    issue = not (pair_valid(key, cert, run=run) and verify(ca, cert, run=run))
    if not issue:
        have = _san_names(cert, run)
        for name in sans:
            if san_entry(name) not in have:
                say(f'SAN не содержит {name}, перевыпуск server cert')
                issue = True
    if issue:
        pub = pki / 'server.pub'
        pub.write_text(_checked(run, ['pki', '--pub', '--in', str(key), '--type', 'ecdsa', '--outform', 'pem'],
                                'pki --pub').stdout)
        done = _checked(run, ['pki', '--issue', '--cacert', str(ca), '--cakey', str(ca_key),
                              '--in', str(pub), '--type', 'pub', '--dn', f'CN={public}',
                              *(f'--san={name}' for name in sans),
                              '--flag', 'serverAuth', '--flag', 'ikeIntermediate',
                              '--lifetime', '1825', '--outform', 'pem'], 'pki --issue')
        pub.unlink()
        cert.write_text(done.stdout)
    _require_pair(key, cert, run)
    _checked(run, ['openssl', 'verify', '-CAfile', str(ca), '-purpose', 'sslserver', str(cert)], 'openssl verify')
    for path, mode in ((ca_key, 0o600), (key, 0o600), (ca, 0o644), (cert, 0o644)):
        path.chmod(mode)
