"""Ed25519 (RFC 8032) in pure Python - used to verify signed agent releases.

Why this exists: the CSP PCs have only Python's standard library plus Flask and
requests, and Python's standard library cannot verify public-key signatures.
Adding a compiled crypto package to every CSP PC (and keeping it compatible with
every Python version in the field) is a bigger risk than carrying ~100 lines of
a well-known reference algorithm. This is the RFC 8032 section 6 reference
implementation, unchanged in logic, with input checks added.

* verify(): what the agent and server call. Not constant-time (it only handles
  PUBLIC data: public key, message, signature).
* sign() / secret_to_public(): used by the release tool on the administrator's
  machine and by tests. The private key never reaches the server or a CSP PC.

Correctness is pinned by tests: the RFC 8032 test vector, round trips, tamper
tests and a cross-check against the `cryptography` library (when installed).
If you prefer a vetted library, replace this file's three functions - nothing
else in the project touches the maths.

The same file lives in agent/ and server/ (a test fails if the copies differ).
"""
import hashlib

_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493


def _sha512(s):
    return hashlib.sha512(s).digest()


def _sha512_modq(s):
    return int.from_bytes(_sha512(s), "little") % _q


def _modp_inv(x):
    return pow(x, _p - 2, _p)


_d = -121665 * _modp_inv(121666) % _p
_modp_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _point_add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _p, (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C, D = 2 * P[3] * Q[3] * _d % _p, 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _point_mul(s, P):
    Q = (0, 1, 1, 0)                       # neutral element
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % _p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % _p != 0:
        return False
    return True


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _modp_inv(_d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _modp_sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_g_y = 4 * _modp_inv(5) % _p
_g_x = _recover_x(_g_y, 0)
_G = (_g_x, _g_y, 1, _g_x * _g_y % _p)


def _point_compress(P):
    zinv = _modp_inv(P[2])
    x = P[0] * zinv % _p
    y = P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _p)


def _secret_expand(secret):
    if len(secret) != 32:
        raise ValueError("an Ed25519 private key is 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    return a, h[32:]


def secret_to_public(secret):
    a, _ = _secret_expand(secret)
    return _point_compress(_point_mul(a, _G))


def sign(secret, msg):
    a, prefix = _secret_expand(secret)
    A = _point_compress(_point_mul(a, _G))
    r = _sha512_modq(prefix + msg)
    Rs = _point_compress(_point_mul(r, _G))
    h = _sha512_modq(Rs + A + msg)
    s = (r + h * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public, msg, signature):
    """True only for a valid signature of `msg` by `public`. Never raises on bad input."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        A = _point_decompress(public)
        if A is None:
            return False
        Rs = signature[:32]
        R = _point_decompress(Rs)
        if R is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _q:
            return False
        h = _sha512_modq(Rs + public + msg)
        sB = _point_mul(s, _G)
        hA = _point_mul(h, A)
        return _point_equal(sB, _point_add(R, hA))
    except Exception:
        return False
