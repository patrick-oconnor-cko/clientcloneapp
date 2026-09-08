"""
RSA key material for minting the destination client's API keys inside a run.

CAT hands an API key's secret back RSA-ENCRYPTED with a public crypto key registered on
the client (`POST /clients/{id}/public-keys`, then `POST /clients/{id}/standalone-reference-
tokens` with that `public_key_id` -> `temporary_secret`). Whoever holds the matching
private key — and only they — can turn that into the `sk_sbox_…` / `pk_sbox_…` value.
The clone tool therefore generates a keypair at APPLY time, registers the public half on
the new client, decrypts the two secrets it gets back, uses the secret key for the webhook
steps in the same run, and shows both values to the operator once. The private key lives
in process memory for the duration of the run and is never written anywhere.

Pure standard library on purpose (the project has no dependencies): Miller-Rabin prime
generation, PKCS#1 RSAPublicKey DER/PEM, and decryption with padding DETECTION — CAT's
padding (PKCS#1 v1.5 vs OAEP) is not documented, so both are tried and the result is
accepted only if it looks like a Checkout API key.
"""
import base64, hashlib, re, secrets

API_KEY_RE = re.compile(r"^(sk|pk)_(sbox_)?[A-Za-z0-9_\-]{10,}$")
PUBLIC_EXPONENT = 65537

# small primes for a cheap first sieve before Miller-Rabin
_SMALL_PRIMES = [p for p in range(3, 2000, 2)
                 if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def _is_probable_prime(n, rounds=40):
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2; r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _random_prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(c):
            return c


class KeyPair:
    """An RSA keypair held in memory. `public_pem` is what CAT is given; nothing else
    leaves this object."""

    def __init__(self, n, e, d, p, q):
        self.n, self.e, self.d, self.p, self.q = n, e, d, p, q
        self.size = (n.bit_length() + 7) // 8

    # -- PKCS#1 RSAPublicKey ::= SEQUENCE { modulus INTEGER, publicExponent INTEGER } ----
    @staticmethod
    def _der_len(n):
        if n < 0x80:
            return bytes([n])
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(b)]) + b

    @classmethod
    def _der_int(cls, v):
        b = v.to_bytes((v.bit_length() + 7) // 8 or 1, "big")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + cls._der_len(len(b)) + b

    @property
    def public_der(self):
        body = self._der_int(self.n) + self._der_int(self.e)
        return b"\x30" + self._der_len(len(body)) + body

    @property
    def public_pem(self):
        b64 = base64.b64encode(self.public_der).decode()
        lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
        return "-----BEGIN RSA PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END RSA PUBLIC KEY-----\n"

    # -- decryption -----------------------------------------------------------------
    def _raw_decrypt(self, ciphertext):
        c = int.from_bytes(ciphertext, "big")
        if c >= self.n:
            raise ValueError("ciphertext out of range for this key")
        return pow(c, self.d, self.n).to_bytes(self.size, "big")

    def decrypt(self, ciphertext_b64):
        """Decrypt CAT's base64 `temporary_secret` / `secret`. Returns (plaintext, padding).

        Tries PKCS#1 v1.5, then OAEP with SHA-1 and SHA-256. A candidate is accepted only
        if it decodes as ASCII and looks like an API key — a wrong padding yields noise,
        never a false positive of that shape.
        """
        em = self._raw_decrypt(base64.b64decode(ciphertext_b64))
        candidates = []
        m = _unpad_pkcs1_v15(em)
        if m is not None:
            candidates.append((m, "PKCS1-v1.5"))
        for h in (hashlib.sha1, hashlib.sha256):
            m = _unpad_oaep(em, h)
            if m is not None:
                candidates.append((m, f"OAEP-{h().name.upper()}"))
        for m, pad in candidates:
            try:
                s = m.decode("ascii").strip()
            except UnicodeDecodeError:
                continue
            if API_KEY_RE.match(s):
                return s, pad
        raise ValueError("decrypted value is not an API key under any supported padding "
                         f"({', '.join(p for _, p in candidates) or 'no padding matched'})")


def _unpad_pkcs1_v15(em):
    if len(em) < 11 or em[0] != 0 or em[1] != 2:
        return None
    sep = em.find(b"\x00", 2)
    if sep < 10:
        return None
    return em[sep + 1:]


def _mgf1(seed, length, h):
    out, counter = b"", 0
    while len(out) < length:
        out += h(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def _unpad_oaep(em, h):
    hlen = h().digest_size
    k = len(em)
    if k < 2 * hlen + 2 or em[0] != 0:
        return None
    masked_seed, masked_db = em[1:1 + hlen], em[1 + hlen:]
    seed = bytes(a ^ b for a, b in zip(masked_seed, _mgf1(masked_db, hlen, h)))
    db = bytes(a ^ b for a, b in zip(masked_db, _mgf1(seed, k - hlen - 1, h)))
    if db[:hlen] != h(b"").digest():
        return None
    i = hlen
    while i < len(db) and db[i] == 0:
        i += 1
    if i >= len(db) or db[i] != 1:
        return None
    return db[i + 1:]


def generate(bits=2048):
    """Generate a fresh RSA keypair. 2048 bits takes a second or two in pure Python."""
    e = PUBLIC_EXPONENT
    while True:
        p = _random_prime(bits // 2)
        q = _random_prime(bits // 2)
        if p == q:
            continue
        n = p * q
        phi = (p - 1) * (q - 1)
        if n.bit_length() != bits or phi % e == 0:
            continue
        d = pow(e, -1, phi)
        return KeyPair(n, e, d, p, q)


# -- encryption, used ONLY by tests to play CAT's part -------------------------------------

def encrypt_pkcs1_v15(pem, message):
    n, e = parse_public_pem(pem)
    k = (n.bit_length() + 7) // 8
    ps = bytes(secrets.randbelow(255) + 1 for _ in range(k - 3 - len(message)))
    em = b"\x00\x02" + ps + b"\x00" + message
    return base64.b64encode(pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")).decode()


def encrypt_oaep(pem, message, h=hashlib.sha256):
    n, e = parse_public_pem(pem)
    k, hlen = (n.bit_length() + 7) // 8, h().digest_size
    db = h(b"").digest() + b"\x00" * (k - len(message) - 2 * hlen - 2) + b"\x01" + message
    seed = secrets.token_bytes(hlen)
    masked_db = bytes(a ^ b for a, b in zip(db, _mgf1(seed, k - hlen - 1, h)))
    masked_seed = bytes(a ^ b for a, b in zip(seed, _mgf1(masked_db, hlen, h)))
    em = b"\x00" + masked_seed + masked_db
    return base64.b64encode(pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")).decode()


def parse_public_pem(pem):
    """PKCS#1 RSAPublicKey PEM -> (n, e). Minimal DER walk; enough for what we emit."""
    b64 = "".join(l for l in pem.strip().splitlines() if not l.startswith("-----"))
    der = base64.b64decode(b64)

    def read_len(buf, i):
        l = buf[i]; i += 1
        if l & 0x80:
            nb = l & 0x7F
            l = int.from_bytes(buf[i:i + nb], "big"); i += nb
        return l, i

    assert der[0] == 0x30
    _, i = read_len(der, 1)
    ints = []
    for _ in range(2):
        assert der[i] == 0x02; i += 1
        l, i = read_len(der, i)
        ints.append(int.from_bytes(der[i:i + l], "big")); i += l
    return ints[0], ints[1]
