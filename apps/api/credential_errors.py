"""Fixed public errors at the credential boundary; preserve cancellation."""
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound

from services.credentials.vault import CredentialConfigurationError


_EXPECTED_HTTP = {
    (422, "CREDENTIAL_PROVIDER_UNSUPPORTED"),
    (404, "CREDENTIAL_NOT_FOUND"),
    (503, "CREDENTIAL_PROVIDER_BUSY"),
}
_CIPHER_CODES = {"CREDENTIAL_STORE_UNAVAILABLE", "CREDENTIAL_INPUT_INVALID"}


@contextmanager
def credential_error_boundary():
    """Do not let lower-level exceptions enter response bodies or tracebacks.

    The caller still owns probe cancellation and cleanup. BaseException is
    deliberately excluded so task cancellation and process controls propagate.
    """
    try:
        yield
    except NoResultFound:
        raise
    except HTTPException as error:
        code = error.detail.get("code") if isinstance(error.detail, dict) else None
        if isinstance(code, str) and (error.status_code, code) in _EXPECTED_HTTP:
            raise HTTPException(error.status_code, {"code": code}) from None
        raise HTTPException(503, {"code": "CREDENTIAL_STORE_UNAVAILABLE"}) from None
    except CredentialConfigurationError as error:
        code = (error.code if isinstance(error.code, str) and error.code in _CIPHER_CODES
                else "CREDENTIAL_STORE_UNAVAILABLE")
        raise HTTPException(503, {"code": code}) from None
    except Exception:
        raise HTTPException(503, {"code": "CREDENTIAL_STORE_UNAVAILABLE"}) from None
