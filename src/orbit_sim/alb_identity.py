"""Verify user claims signed by a configured AWS Application Load Balancer."""

import re
from base64 import urlsafe_b64decode
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from http.client import HTTPException, HTTPSConnection
from threading import Lock
from time import monotonic, time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256R1,
    EllipticCurvePublicKey,
)
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from jwt.api_jws import PyJWS
from jwt.exceptions import PyJWTError

from .files import parse_json_object

_ARN = re.compile(r"arn:aws:elasticloadbalancing:([a-z]{2}(?:-[a-z0-9]+)+-\d):\d{12}:"
                  r"loadbalancer/app/[a-zA-Z0-9-]{1,32}/[0-9a-f]{16}")
_DNS = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                  r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_KEY_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_TOKEN = re.compile(r"(?:[A-Za-z0-9_-]+={0,2}\.){2}[A-Za-z0-9_-]+={0,2}")
_MAX_TOKEN = 16 * 1024
_MAX_PEM = 4096
_KEY_TTL_S = 900
_NEGATIVE_TTL_S = 20


class InvalidIdentity(Exception):
    """No trusted user identity could be established."""


class IdentityUnavailable(Exception):
    """AWS signing keys could not be retrieved safely."""


@dataclass(frozen=True, slots=True)
class AlbConfig:
    origin: str
    signer: str
    issuer: str
    client: str

    def __post_init__(self) -> None:
        if not self.origin.startswith("https://") or _DNS.fullmatch(self.origin[8:]) is None:
            raise ValueError("ORBIT_PUBLIC_ORIGIN must be a canonical HTTPS origin with a DNS hostname.")
        if _ARN.fullmatch(self.signer) is None:
            raise ValueError("ORBIT_ALB_ARN must identify a commercial AWS Application Load Balancer.")
        issuer = urlsplit(self.issuer)
        if (not self.issuer.isascii() or not self.issuer.isprintable() or issuer.scheme != "https"
                or not issuer.hostname or issuer.username is not None or issuer.query or issuer.fragment):
            raise ValueError("ORBIT_OIDC_ISSUER must identify the HTTPS issuer.")
        if re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", self.client) is None:
            raise ValueError("ORBIT_OIDC_CLIENT_ID must identify the expected client.")

    @property
    def host(self) -> str:
        return self.origin[8:]

    @property
    def region(self) -> str:
        return self.signer.split(":", 5)[3]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "AlbConfig":
        return cls(*(environment.get(name, "") for name in (
            "ORBIT_PUBLIC_ORIGIN", "ORBIT_ALB_ARN", "ORBIT_OIDC_ISSUER", "ORBIT_OIDC_CLIENT_ID",
        )))


class AlbKeys:
    """Cache AWS P-256 signing keys and bound cache-miss traffic."""

    def __init__(self, region: str):
        self.hostname = f"public-keys.auth.elb.{region}.amazonaws.com"
        self.entries: OrderedDict[str, tuple[float, EllipticCurvePublicKey | None]] = OrderedDict()
        self.attempts: deque[float] = deque()
        self.lock = Lock()
        self.inflight = False

    def __call__(self, key_id: str) -> EllipticCurvePublicKey:
        if _KEY_ID.fullmatch(key_id) is None:
            raise InvalidIdentity
        with self.lock:
            now = monotonic()
            cached = self.entries.get(key_id)
            if cached is not None and cached[0] > now:
                self.entries.move_to_end(key_id)
                if cached[1] is None:
                    raise InvalidIdentity
                return cached[1]
            while self.attempts and self.attempts[0] <= now - 10:
                self.attempts.popleft()
            if self.inflight or len(self.attempts) >= 4:
                raise IdentityUnavailable
            self.attempts.append(now)
            self.inflight = True
        try:
            key = self.fetch(key_id)
            with self.lock:
                ttl = _KEY_TTL_S if key is not None else _NEGATIVE_TTL_S
                self.entries[key_id] = (monotonic() + ttl, key)
                self.entries.move_to_end(key_id)
                if len(self.entries) > 16:
                    self.entries.popitem(last=False)
        finally:
            with self.lock:
                self.inflight = False
        if key is None:
            raise InvalidIdentity
        return key

    def fetch(self, key_id: str) -> EllipticCurvePublicKey | None:
        client = HTTPSConnection(self.hostname, timeout=3)
        try:
            client.request("GET", f"/{key_id}")
            response = client.getresponse()
            if response.status == 404:
                return None
            if response.status != 200:
                raise IdentityUnavailable
            pem = response.read(_MAX_PEM + 1)
            if len(pem) > _MAX_PEM:
                raise IdentityUnavailable
            key = load_pem_public_key(pem)
        except (OSError, ValueError, HTTPException) as exc:
            raise IdentityUnavailable from exc
        finally:
            client.close()
        if not isinstance(key, EllipticCurvePublicKey) or not isinstance(key.curve, SECP256R1):
            raise IdentityUnavailable
        return key


class AlbIdentity:
    """Return a stable pseudonymous owner for verified user-info claims."""

    def __init__(
        self, config: AlbConfig, resolve_key: Callable[[str], EllipticCurvePublicKey] | None = None,
        now: Callable[[], float] = time,
    ):
        self.config = config
        self.resolve_key = resolve_key if resolve_key is not None else AlbKeys(config.region)
        self.now = now
        self.jws = PyJWS()

    def owner(self, token: str) -> str:
        if len(token) > _MAX_TOKEN or _TOKEN.fullmatch(token) is None:
            raise InvalidIdentity
        segment = token.split(".", 1)[0]
        try:
            header = parse_json_object(
                urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode("utf-8"), "identity header",
            )
        except ValueError as exc:
            raise InvalidIdentity from exc
        key_id, expires = header.get("kid"), header.get("exp")
        if (header.get("alg") != "ES256" or header.get("signer") != self.config.signer
                or header.get("iss") != self.config.issuer or header.get("client") != self.config.client
                or not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None
                or not self._current_expiry(expires)):
            raise InvalidIdentity
        key = self.resolve_key(key_id)
        try:
            payload = self.jws.decode(token, key, algorithms=["ES256"])
            claims = parse_json_object(payload.decode("utf-8"), "signed identity claims")
        except (PyJWTError, ValueError) as exc:
            raise InvalidIdentity from exc
        subject = claims.get("sub")
        if (not isinstance(subject, str) or not 1 <= len(subject) <= 255
                or not subject.isascii() or not subject.isprintable() or not self._current_expiry(expires)):
            raise InvalidIdentity
        identity = f"{self.config.issuer}\0{self.config.client}\0{subject}".encode()
        return "oidc-" + sha256(identity).hexdigest()

    def _current_expiry(self, value: object) -> bool:
        if type(value) is int:
            expiry = value
        elif isinstance(value, str) and re.fullmatch(r"\d{1,11}", value, flags=re.ASCII):
            expiry = int(value)
        else:
            return False
        return self.now() < expiry <= 99_999_999_999
