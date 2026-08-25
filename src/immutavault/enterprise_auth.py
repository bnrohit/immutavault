from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .enterprise_config import EnterpriseConfig, OIDCConfig, ROLE_ORDER


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_b64(value: dict[str, Any]) -> str:
    return _b64url_encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


@dataclass(frozen=True)
class Identity:
    subject: str
    name: str
    role: str
    tenants: tuple[str, ...]
    source: str
    mfa: bool
    tenant_id: str | None = None

    def allows_tenant(self, tenant_id: str) -> bool:
        return "*" in self.tenants or tenant_id in self.tenants

    def public(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "name": self.name,
            "role": self.role,
            "tenants": list(self.tenants),
            "source": self.source,
            "mfa": self.mfa,
            "tenant_id": self.tenant_id,
        }


class SignedToken:
    """Small HMAC-signed token for portal sessions, login state and WS tickets."""

    def __init__(self, secret: str) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("OIDC session secret must contain at least 32 bytes")
        self.secret = secret.encode()

    @classmethod
    def from_env(cls, env_name: str) -> "SignedToken":
        value = os.getenv(env_name)
        if not value:
            raise RuntimeError(f"{env_name} is required when enterprise OIDC/session signing is enabled")
        return cls(value)

    def encode(self, payload: dict[str, Any]) -> str:
        body = _json_b64(payload)
        signature = _b64url_encode(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        return body + "." + signature

    def decode(self, token: str, *, expected_type: str | None = None) -> dict[str, Any]:
        try:
            body, signature = token.split(".", 1)
        except ValueError as exc:
            raise PermissionError("invalid signed token") from exc
        expected = _b64url_encode(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise PermissionError("invalid signed token signature")
        try:
            payload = json.loads(_b64url_decode(body))
        except Exception as exc:
            raise PermissionError("invalid signed token payload") from exc
        if int(payload.get("exp", 0)) < int(time.time()):
            raise PermissionError("signed token expired")
        if expected_type and payload.get("typ") != expected_type:
            raise PermissionError("signed token type mismatch")
        return payload

    def identity_token(self, identity: Identity, *, minutes: int) -> str:
        now = int(time.time())
        return self.encode({
            "typ": "session",
            "iat": now,
            "exp": now + minutes * 60,
            "sub": identity.subject,
            "name": identity.name,
            "role": identity.role,
            "tenants": list(identity.tenants),
            "source": identity.source,
            "mfa": identity.mfa,
            "tenant_id": identity.tenant_id,
        })

    def identity_from_token(self, token: str) -> Identity:
        value = self.decode(token, expected_type="session")
        role = str(value.get("role", "viewer"))
        if role not in ROLE_ORDER:
            raise PermissionError("invalid session role")
        tenants = tuple(str(item) for item in value.get("tenants") or [])
        if not tenants:
            raise PermissionError("session has no tenant scope")
        return Identity(
            subject=str(value.get("sub") or ""),
            name=str(value.get("name") or value.get("sub") or "OIDC user"),
            role=role,
            tenants=tenants,
            source=str(value.get("source") or "oidc"),
            mfa=bool(value.get("mfa", False)),
            tenant_id=str(value.get("tenant_id")) if value.get("tenant_id") else None,
        )


class OIDCClient:
    def __init__(self, cfg: EnterpriseConfig, signer: SignedToken) -> None:
        self.enterprise = cfg
        self.cfg: OIDCConfig = cfg.oidc
        self.signer = signer
        self._metadata: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_loaded_at = 0.0

    @staticmethod
    def _fetch_json(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        if not url.startswith("https://"):
            raise RuntimeError("OIDC metadata/token/JWKS endpoints must use https")
        request = Request(url, data=data, headers=headers or {})
        with urlopen(request, timeout=15) as response:  # nosec - URL is rooted in operator-configured OIDC issuer
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"OIDC endpoint returned HTTP {response.status}")
            value = json.loads(response.read(4 * 1024 * 1024))
        if not isinstance(value, dict):
            raise RuntimeError("OIDC endpoint returned a non-object JSON value")
        return value

    def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
            metadata = self._fetch_json(url)
            if str(metadata.get("issuer") or "").rstrip("/") != self.cfg.issuer.rstrip("/"):
                raise RuntimeError("OIDC discovery issuer does not match configured issuer")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not str(metadata.get(key) or "").startswith("https://"):
                    raise RuntimeError(f"OIDC discovery is missing a secure {key}")
            self._metadata = metadata
        return self._metadata

    def _jwks_value(self, *, force: bool = False) -> dict[str, Any]:
        if force or self._jwks is None or time.time() - self._jwks_loaded_at > 3600:
            self._jwks = self._fetch_json(str(self.metadata()["jwks_uri"]))
            self._jwks_loaded_at = time.time()
        return self._jwks

    def begin_login(self, *, return_to: str = "/") -> tuple[str, str]:
        verifier = _b64url_encode(secrets.token_bytes(48))
        challenge = _b64url_encode(hashlib.sha256(verifier.encode()).digest())
        state = _b64url_encode(secrets.token_bytes(24))
        nonce = _b64url_encode(secrets.token_bytes(24))
        now = int(time.time())
        state_token = self.signer.encode({
            "typ": "oidc_state",
            "iat": now,
            "exp": now + 600,
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "return_to": return_to if return_to.startswith("/") and not return_to.startswith("//") else "/",
        })
        query = {
            "client_id": self.cfg.client_id,
            "response_type": "code",
            "redirect_uri": self.cfg.redirect_uri,
            "response_mode": "query",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return str(self.metadata()["authorization_endpoint"]) + "?" + urlencode(query), state_token

    def complete_login(self, *, code: str, state: str, state_token: str) -> tuple[Identity, str]:
        saved = self.signer.decode(state_token, expected_type="oidc_state")
        if not state or not hmac.compare_digest(state, str(saved.get("state") or "")):
            raise PermissionError("OIDC state mismatch")
        if not code:
            raise PermissionError("OIDC authorization code is missing")

        form = {
            "grant_type": "authorization_code",
            "client_id": self.cfg.client_id,
            "code": code,
            "redirect_uri": self.cfg.redirect_uri,
            "code_verifier": str(saved["verifier"]),
        }
        if self.cfg.client_secret_env:
            secret = os.getenv(self.cfg.client_secret_env)
            if not secret:
                raise RuntimeError(f"{self.cfg.client_secret_env} is required for OIDC token exchange")
            form["client_secret"] = secret
        token_response = self._fetch_json(
            str(self.metadata()["token_endpoint"]),
            data=urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        id_token = str(token_response.get("id_token") or "")
        if not id_token:
            raise PermissionError("OIDC provider did not return an ID token")
        claims = self.validate_id_token(id_token, nonce=str(saved["nonce"]))
        identity = self.identity_from_claims(claims)
        return identity, str(saved.get("return_to") or "/")

    @staticmethod
    def _der_length(value: int) -> bytes:
        if value < 128:
            return bytes([value])
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(raw)]) + raw

    @classmethod
    def _der_integer(cls, raw: bytes) -> bytes:
        raw = raw.lstrip(b"\x00") or b"\x00"
        if raw[0] & 0x80:
            raw = b"\x00" + raw
        return b"\x02" + cls._der_length(len(raw)) + raw

    @classmethod
    def _rsa_public_key_pem(cls, jwk: dict[str, Any]) -> bytes:
        n = _b64url_decode(str(jwk["n"]))
        e = _b64url_decode(str(jwk["e"]))
        rsa = cls._der_integer(n) + cls._der_integer(e)
        rsa = b"\x30" + cls._der_length(len(rsa)) + rsa
        algorithm = bytes.fromhex("300d06092a864886f70d0101010500")
        bitstring_body = b"\x00" + rsa
        bitstring = b"\x03" + cls._der_length(len(bitstring_body)) + bitstring_body
        spki_body = algorithm + bitstring
        spki = b"\x30" + cls._der_length(len(spki_body)) + spki_body
        encoded = base64.encodebytes(spki).replace(b"\n\n", b"\n")
        return b"-----BEGIN PUBLIC KEY-----\n" + encoded + b"-----END PUBLIC KEY-----\n"

    def _verify_rs256(self, signing_input: bytes, signature: bytes, jwk: dict[str, Any]) -> None:
        pem = self._rsa_public_key_pem(jwk)
        import tempfile
        with tempfile.NamedTemporaryFile(mode="wb") as key_file, tempfile.NamedTemporaryFile(mode="wb") as sig_file:
            key_file.write(pem); key_file.flush()
            sig_file.write(signature); sig_file.flush()
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", key_file.name, "-signature", sig_file.name],
                input=signing_input,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        if result.returncode != 0 or b"Verified OK" not in result.stdout:
            raise PermissionError("OIDC ID token signature validation failed")

    def validate_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        try:
            header_raw, payload_raw, signature_raw = token.split(".")
            header = json.loads(_b64url_decode(header_raw))
            claims = json.loads(_b64url_decode(payload_raw))
            signature = _b64url_decode(signature_raw)
        except Exception as exc:
            raise PermissionError("malformed OIDC ID token") from exc
        if header.get("alg") != "RS256":
            raise PermissionError("only RS256 OIDC ID tokens are accepted")
        kid = str(header.get("kid") or "")
        if not kid:
            raise PermissionError("OIDC ID token is missing kid")

        def find_key(jwks: dict[str, Any]) -> dict[str, Any] | None:
            for item in jwks.get("keys") or []:
                if str(item.get("kid") or "") == kid and item.get("kty") == "RSA":
                    return item
            return None

        jwk = find_key(self._jwks_value())
        if jwk is None:
            jwk = find_key(self._jwks_value(force=True))
        if jwk is None:
            raise PermissionError("OIDC signing key is unknown")
        self._verify_rs256((header_raw + "." + payload_raw).encode(), signature, jwk)

        now = int(time.time())
        skew = 90
        issuer = str(claims.get("iss") or "").rstrip("/")
        if issuer != str(self.metadata()["issuer"]).rstrip("/"):
            raise PermissionError("OIDC issuer mismatch")
        audience = claims.get("aud")
        audiences = [str(audience)] if isinstance(audience, str) else [str(x) for x in (audience or [])]
        if self.cfg.client_id not in audiences:
            raise PermissionError("OIDC audience mismatch")
        if int(claims.get("exp", 0)) < now - skew:
            raise PermissionError("OIDC ID token expired")
        if int(claims.get("nbf", 0) or 0) > now + skew:
            raise PermissionError("OIDC ID token is not active yet")
        if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise PermissionError("OIDC nonce mismatch")
        tenant_id = str(claims.get("tid") or "")
        if self.cfg.allowed_tenant_ids and tenant_id not in self.cfg.allowed_tenant_ids:
            raise PermissionError("OIDC tenant is not authorized")
        return claims

    @staticmethod
    def _highest_role(roles: list[str], default_role: str) -> str:
        candidates = [default_role, *[role for role in roles if role in ROLE_ORDER]]
        return max(candidates, key=lambda role: ROLE_ORDER[role])

    def identity_from_claims(self, claims: dict[str, Any]) -> Identity:
        groups = {str(item) for item in claims.get("groups") or []}
        app_roles = {str(item) for item in claims.get("roles") or []}
        mapped_roles = [self.cfg.group_role_map[g] for g in groups if g in self.cfg.group_role_map]
        mapped_roles += [self.cfg.app_role_map[r] for r in app_roles if r in self.cfg.app_role_map]
        role = self._highest_role(mapped_roles, self.cfg.default_role)

        scopes: set[str] = set(self.cfg.default_tenants)
        for group in groups:
            scopes.update(self.cfg.group_tenant_map.get(group, []))
        for app_role in app_roles:
            scopes.update(self.cfg.app_role_tenant_map.get(app_role, []))
        if not scopes:
            raise PermissionError("OIDC identity has no Immutavault tenant scope")
        known = self.enterprise.tenant_ids()
        if any(scope != "*" and scope not in known for scope in scopes):
            raise PermissionError("OIDC identity resolved an unknown tenant scope")

        amr = {str(item).lower() for item in claims.get("amr") or []}
        acrs = {str(item) for item in claims.get("acrs") or []}
        mfa = bool({"mfa", "ngcmfa"} & amr)
        if self.cfg.required_acrs:
            mfa = mfa or bool(set(self.cfg.required_acrs) & acrs)
        if self.cfg.require_mfa and not mfa:
            raise PermissionError(
                "MFA evidence is required; configure Entra to emit amr (mfa/ngcmfa) or a required authentication-context ID"
            )

        tid = str(claims.get("tid") or "") or None
        oid = str(claims.get("oid") or claims.get("sub") or "")
        if not oid:
            raise PermissionError("OIDC token has no stable subject identifier")
        subject = f"oidc:{tid or 'tenant'}:{oid}"
        name = str(claims.get("name") or claims.get("preferred_username") or oid)
        return Identity(
            subject=subject,
            name=name,
            role=role,
            tenants=tuple(sorted(scopes)),
            source="oidc",
            mfa=mfa,
            tenant_id=tid,
        )


def identity_to_session_payload(identity: Identity, *, minutes: int) -> dict[str, Any]:
    now = int(time.time())
    return {
        "typ": "session",
        "iat": now,
        "exp": now + minutes * 60,
        "sub": identity.subject,
        "name": identity.name,
        "role": identity.role,
        "tenants": list(identity.tenants),
        "source": identity.source,
        "mfa": identity.mfa,
        "tenant_id": identity.tenant_id,
    }
