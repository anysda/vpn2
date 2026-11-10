"""PKI: openssl, index.txt, tls-crypt и openssl.cnf для OpenVPN (stdlib only).

Внешняя программа вызывается только через `run=`: тесты подменяют её.
Проверки, которые отвечают да/нет (`key_valid`, `cert_valid`, `pair_valid`,
`verify`), не бросают: любой отказ openssl для них - False. Остальные при отказе
бросают PkiError; в тексте только программа и последняя строка stderr - ни
аргументов, ни входных данных, значит приватный ключ в исключение не попадает.
"""
import re
import subprocess
from typing import NamedTuple


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


# --- место для transaction и build_openvpn (следующий шаг) ---
