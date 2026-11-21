import io
import json
import unittest
from base64 import urlsafe_b64encode
from importlib.util import find_spec
from unittest.mock import Mock

from orbit_sim.web import JobClient, create_app

_HAS_HOSTED = find_spec("jwt") is not None and find_spec("cryptography") is not None
if _HAS_HOSTED:
    from cryptography.hazmat.primitives.asymmetric.ec import (
        ECDSA,
        SECP256R1,
        generate_private_key,
    )
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.hashes import SHA256

    from orbit_sim.alb_identity import (
        AlbConfig,
        AlbIdentity,
        AlbKeys,
        IdentityUnavailable,
        InvalidIdentity,
    )

_ORIGIN = "https://orbit.example.com"
_SIGNER = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/orbit/0123456789abcdef"
_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
_CLIENT = "orbit-client"
_KID = "01234567-1234-1234-1234-0123456789ab"


@unittest.skipUnless(_HAS_HOSTED, "install the hosted extra to verify signed ALB identities")
class AlbIdentityTests(unittest.TestCase):
    def setUp(self):
        self.config = AlbConfig(_ORIGIN, _SIGNER, _ISSUER, _CLIENT)
        self.key = generate_private_key(SECP256R1())
        self.resolve = Mock(return_value=self.key.public_key())
        self.identity = AlbIdentity(self.config, self.resolve, lambda: 1000)

    def token(self, subject="user-a", header_overrides=None, signer=None, claims=None):
        header = {"alg": "ES256", "kid": _KID, "signer": _SIGNER, "iss": _ISSUER, "client": _CLIENT, "exp": "1030"}
        header.update(header_overrides or {})
        payload = claims if claims is not None else json.dumps({"sub": subject, "email": "ignored@example.com"})
        # ALB signs the encoded segments inclusive of their base64url padding.
        signing_input = urlsafe_b64encode(json.dumps(header).encode()) + b"." + urlsafe_b64encode(payload.encode())
        der = (signer or self.key).sign(signing_input, ECDSA(SHA256()))
        r, s = decode_dss_signature(der)
        return (signing_input + b"." + urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))).decode()

    def test_padded_signed_identity_is_stable_and_distinct_for_each_subject(self):
        owner = self.identity.owner(self.token())
        self.assertRegex(owner, r"^oidc-[0-9a-f]{64}$")
        self.assertEqual(owner, self.identity.owner(self.token(claims='{"sub":"user-a","email":"changed"}')))
        self.assertNotEqual(owner, self.identity.owner(self.token("user-b")))
        self.resolve.assert_called_with(_KID)

    def test_header_mismatches_are_rejected_before_network_key_lookup(self):
        for wrong in ({"signer": "other"}, {"alg": "HS256"}, {"iss": "other"}, {"client": "other"},
                      {"kid": "../test"}, {"exp": 1000}, {"exp": True}, {"exp": "1e50"}):
            with self.subTest(wrong=wrong), self.assertRaises(InvalidIdentity):
                self.identity.owner(self.token(header_overrides=wrong))
        self.resolve.assert_not_called()

    def test_invalid_signature_and_ambiguous_or_missing_subjects_are_rejected(self):
        other_key = generate_private_key(SECP256R1())
        for token in (self.token(signer=other_key), self.token(claims='{"sub":"a","sub":"b"}'),
                      self.token(claims='{"name":"missing"}'), self.token(subject={"arbitrary": "object"})):
            with self.subTest(), self.assertRaises(InvalidIdentity):
                self.identity.owner(token)

    def test_malformed_or_combined_headers_cannot_reach_key_lookup(self):
        for token in ("", "A.B.C", "x" * 17000, self.token() + "," + self.token()):
            with self.subTest(), self.assertRaises(InvalidIdentity):
                self.identity.owner(token)
        self.resolve.assert_not_called()

    def test_signing_key_cache_limits_duplicate_and_unknown_aws_requests(self):
        keys = AlbKeys("us-east-1")
        self.assertEqual(keys.hostname, "public-keys.auth.elb.us-east-1.amazonaws.com")
        keys.fetch = Mock(side_effect=[self.key.public_key(), None, None, None])
        self.assertEqual(keys(_KID), keys(_KID))
        for value in range(3):
            other = f"01234567-1234-1234-1234-{value:012x}"
            with self.assertRaises(InvalidIdentity):
                keys(other)
            with self.assertRaises(InvalidIdentity):
                keys(other)
        with self.assertRaises(IdentityUnavailable):
            keys("01234567-1234-1234-1234-000000000004")
        self.assertEqual(keys.fetch.call_count, 4)

    def request(self, app, path="/", method="GET", body=b"", **headers):
        response = {}

        def start(status, response_headers):
            response.update(status=status, headers=dict(response_headers))

        environment = {
            "HTTP_HOST": self.config.host,
            "PATH_INFO": path,
            "REQUEST_METHOD": method,
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        response["body"] = b"".join(app(environment | headers, start))
        return response

    def test_hosted_gateway_requires_verified_claims_and_https_origin(self):
        jobs = Mock(spec=JobClient)
        app = create_app(8765, jobs, self.identity)
        self.assertEqual(self.request(app)["status"], "401 Unauthorized")
        self.assertEqual(self.request(app, HTTP_X_AMZN_OIDC_IDENTITY="unsigned")["status"], "401 Unauthorized")
        other_key = generate_private_key(SECP256R1())
        self.assertEqual(
            self.request(app, HTTP_X_AMZN_OIDC_DATA=self.token(signer=other_key))["status"], "401 Unauthorized",
        )
        token = self.token()
        response = self.request(app, HTTP_X_AMZN_OIDC_DATA=token)
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["headers"]["Strict-Transport-Security"], "max-age=31536000")
        self.assertEqual(self.request(app, "/healthz", HTTP_HOST="10.1.0.12")["status"], "200 OK")
        self.assertEqual(self.request(app, HTTP_HOST="other.example.com", HTTP_X_AMZN_OIDC_DATA=token)["status"],
                         "403 Forbidden")
        for origin in ("https://other.example.com", "http://orbit.example.com", None):
            headers = {} if origin is None else {"HTTP_ORIGIN": origin}
            response = self.request(app, "/api/jobs", "POST", HTTP_X_AMZN_OIDC_DATA=token, **headers)
            self.assertEqual(response["status"], "403 Forbidden")
        self.assertEqual(self.request(
            app, "/api/run", "POST", HTTP_X_AMZN_OIDC_DATA=token, HTTP_ORIGIN=self.config.origin,
        )["status"], "409 Conflict")
        jobs.forward.assert_not_called()

    def test_job_ownership_is_derived_from_signed_subject_for_every_operation(self):
        jobs = Mock(spec=JobClient)
        jobs.forward.return_value = ("200 OK", "application/json", b"{}")
        app = create_app(8765, jobs, self.identity)
        token_a, token_b = self.token(), self.token("user-b")
        a, b = self.identity.owner(token_a), self.identity.owner(token_b)
        example = self.request(app, "/api/examples/hohmann", HTTP_X_AMZN_OIDC_DATA=token_a)["body"]
        key = "valid-request-key-1"
        self.request(app, "/api/jobs", "POST", example, HTTP_IDEMPOTENCY_KEY=key,
                     HTTP_X_AMZN_OIDC_DATA=token_a, HTTP_ORIGIN=_ORIGIN, HTTP_X_ORBIT_OWNER=b)
        jobs.forward.assert_called_once_with("POST", "/jobs", example, key, owner=a)
        path = "/api/jobs/01234567-1234-1234-1234-0123456789ab"
        for method, route in (("GET", path), ("DELETE", path), ("GET", f"{path}/telemetry")):
            self.request(app, route, method, HTTP_X_AMZN_OIDC_DATA=token_b, HTTP_ORIGIN=_ORIGIN,
                         HTTP_X_ORBIT_OWNER=a, HTTP_X_AMZN_OIDC_IDENTITY="user-a")
            jobs.forward.assert_called_with(method, route.removeprefix("/api"), owner=b)

    def test_key_outage_fails_closed_without_accessing_the_job_service(self):
        jobs = Mock(spec=JobClient)
        app = create_app(8765, jobs, self.identity)
        self.resolve.side_effect = IdentityUnavailable
        result = self.request(app, "/api/capabilities", HTTP_X_AMZN_OIDC_DATA=self.token())
        self.assertEqual(result["status"], "503 Service Unavailable")
        jobs.forward.assert_not_called()
        with self.assertRaisesRegex(ValueError, "requires the Java"):
            create_app(8765, identity=self.identity)
