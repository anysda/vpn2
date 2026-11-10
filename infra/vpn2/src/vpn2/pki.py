"""PKI: openssl, index.txt, tls-crypt и openssl.cnf для OpenVPN (stdlib only).

Внешняя программа вызывается только через `run=`: тесты подменяют её.
Проверки, которые отвечают да/нет (`key_valid`, `cert_valid`, `pair_valid`,
`verify`), не бросают: любой отказ openssl для них - False. Остальные при отказе
бросают PkiError; в тексте только программа и последняя строка stderr - ни
аргументов, ни входных данных, значит приватный ключ в исключение не попадает.
"""
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from vpn2.fsutil import atomic

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


# --- место для transaction и build_openvpn (следующий шаг) ---
