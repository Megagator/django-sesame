import base64
import datetime
import hashlib
import hmac
import logging
import re
import struct
import time

from . import packers, settings

__all__ = ["create_token", "detect_token", "parse_token"]

logger = logging.getLogger("sesame")

TIMESTAMP_OFFSET = 1577836800  # 2020-01-01T00:00:00Z


def pack_timestamp():
    """
    When SESAME_MAX_AGE is enabled, encode the time in seconds since the epoch.

    Return bytes.

    """
    if settings.MAX_AGE is None:
        return b""
    timestamp = int(time.time()) - TIMESTAMP_OFFSET
    return struct.pack("!i", timestamp)


def unpack_timestamp(data):
    """
    When SESAME_MAX_AGE is enabled, extract the timestamp and calculate the age.

    Return an age in seconds or None and the remaining bytes.

    """
    if settings.MAX_AGE is None:
        return None, data
    # If data contains less than 4 bytes, this raises struct.error.
    (timestamp,), data = struct.unpack("!i", data[:4]), data[4:]
    return int(time.time()) - TIMESTAMP_OFFSET - timestamp, data


HASH_SIZES = {
    "pbkdf2_sha256": 44,
    "pbkdf2_sha1": 28,
    "argon2": 22,  # in Argon2 v1.3; previously 86
    "bcrypt_sha256": 31,  # salt (22) + hash (31)
    "bcrypt": 31,  # salt (22) + hash (31)
    "sha1": 40,  # hex, not base64
    "md5": 32,  # hex, not base64
    "crypt": 11,  # salt (2) + hash (11)
}


def get_revocation_key(user):
    """
    When the value returned by this method changes, this revokes tokens.

    It is derived from the hashed password so that changing the password
    revokes tokens.

    It may be derived from the email so that changing the email revokes tokens
    too.

    For one-time tokens, it also contains the last login datetime so that
    logging in revokes existing tokens.

    """
    data = ""

    # Tokens generated by django-sesame are more likely to leak than hashed
    # passwords. To minimize the information tokens might be revealing, we'd
    # like to use only hashes, excluding salts, as suggested in issue #40.

    # Since we're hashing the result again with a cryptographic hash function,
    # this isn't supposed to make a difference in practice. But it alleviates
    # concerns about sending data derived from hashed passwords into the wild.

    # Hashed passwords may be in various formats:
    # 1. "[<algorithm>$]?[<parameters>$]*[<salt>$?]?<hash>", if set_password()
    #    was called with a built-in hasher. Unfortunatly, the bcrypt (and
    #    crypt) hashers don't include a "$" between the salt and the hash, so
    #    we can't split on this marker. Instead we hardcode hash lengths.
    # 2. "!<40 random characters>", if set_unusable_password() was called.
    # 3. Anything else, if set_password() was called with a custom hasher or
    #    if a custom authentication backend is used.

    # An alternative would be to rely on user.get_session_auth_hash(), which
    # has the advantage of being a public API. It's a HMAC-SHA256 of the whole
    # password hash. However, it's designed for a slightly different purpose,
    # so I'm not comfortable reusing it. Also, for clarity, I don't want to
    # chain more cryptographic operations than needed.

    if settings.INVALIDATE_ON_PASSWORD_CHANGE and user.password is not None:
        algorithm = user.password.partition("$")[0]
        try:
            hash_size = HASH_SIZES[algorithm]
        except KeyError:
            data += user.password
        else:
            data += user.password[-hash_size:]

    if settings.INVALIDATE_ON_EMAIL_CHANGE:
        data += getattr(user, user.get_email_field_name())

    if settings.ONE_TIME and user.last_login is not None:
        data += user.last_login.isoformat()

    return data.encode()


def sign(data, key, size):
    """
    Create a MAC with keyed hashing.

    """
    return hashlib.blake2b(
        data,
        digest_size=size,
        key=key,
        person=b"sesame.tokens_v2",
    ).digest()


def create_token(user, scope=""):
    """
    Create a v2 signed token for a user.

    """
    primary_key = packers.packer.pack_pk(getattr(user, settings.PRIMARY_KEY_FIELD))
    timestamp = pack_timestamp()
    revocation_key = get_revocation_key(user)

    signature = sign(
        primary_key + timestamp + revocation_key + scope.encode(),
        settings.SIGNING_KEY,
        settings.SIGNATURE_SIZE,
    )

    # If the revocation key changes, the signature becomes invalid, so we
    # don't need to include a hash of the revocation key in the token.
    data = primary_key + timestamp + signature
    token = base64.urlsafe_b64encode(data).rstrip(b"=")
    return token.decode()


def parse_token(token, get_user, scope="", max_age=None):
    """
    Obtain a user from a v2 signed token.

    """
    token = token.encode()

    # Below, error messages should give a hint to developers debugging apps
    # but remain sufficiently generic for the common situation where tokens
    # get truncated by accident.

    try:
        data = base64.urlsafe_b64decode(token + b"=" * (-len(token) % 4))
    except Exception:
        logger.debug("Bad token: cannot decode token")
        return None

    # Extract user primary key, token age, and signature from token.

    try:
        user_pk, timestamp_and_signature = packers.packer.unpack_pk(data)
    except Exception:
        logger.debug("Bad token: cannot extract primary key")
        return None

    try:
        age, signature = unpack_timestamp(timestamp_and_signature)
    except Exception:
        logger.debug("Bad token: cannot extract timestamp")
        return None

    if len(signature) != settings.SIGNATURE_SIZE:
        logger.debug("Bad token: cannot extract signature")
        return None

    # Since we don't include the revocation key in the token, we need to fetch
    # the user in the database before we can verify the signature. Usually,
    # it's best to verify the signature before doing anything with a message.

    # An attacker could craft tokens to fetch arbitrary users by primary key,
    # like they can fetch arbitrary users by username on a login form.
    # Determining whether there's a user with a given primary key via a timing
    # attack is acceptable within django-sesame's threat model.

    # Check if token is expired. Perform this check first, because it's fast.

    if max_age is None:
        max_age = settings.MAX_AGE
    elif settings.MAX_AGE is None:
        logger.warning(
            "Ignoring max_age argument; "
            "it isn't supported when SESAME_MAX_AGE = None"
        )
    elif isinstance(max_age, datetime.timedelta):
        max_age = max_age.total_seconds()
    if age is not None and age >= max_age:
        logger.debug("Expired token: age = %d seconds", age)
        return None

    # Check if user exists and can log in.

    user = get_user(user_pk)
    if user is None:
        logger.debug(
            "Unknown or inactive user: %s = %r",
            settings.PRIMARY_KEY_FIELD,
            user_pk,
        )
        return None

    # Check if signature is valid

    primary_key_and_timestamp = data[: -settings.SIGNATURE_SIZE]
    revocation_key = get_revocation_key(user)
    for verification_key in settings.VERIFICATION_KEYS:
        expected_signature = sign(
            primary_key_and_timestamp + revocation_key + scope.encode(),
            verification_key,
            settings.SIGNATURE_SIZE,
        )
        if hmac.compare_digest(signature, expected_signature):
            log_scope = "in default scope" if scope == "" else f"in scope {scope}"
            logger.debug("Valid token for user %s %s", user, log_scope)
            return user

    log_scope = "in default scope" if scope == "" else f"in scope {scope}"
    logger.debug("Invalid token for user %s %s", user, log_scope)
    return None


# Tokens are arbitrary Base64-encoded bytestrings. Their size depends on
# SESAME_PACKER, SESAME_MAX_AGE, and SESAME_SIGNATURE_SIZE. Defaults are:
# - without SESAME_MAX_AGE: 4 + 10 = 14 bytes = 19 Base64 characters.
# - with SESAME_MAX_AGE: 4 + 4 + 10 = 18 bytes = 24 Base64 characters.
# Minimum "sensible" size is 1 + 0 + 2 = 3 bytes = 4 Base64 characters.
token_re = re.compile(r"[A-Za-z0-9-_]{4,}")


def detect_token(token):
    """
    Tell whether token may be a v2 signed token.

    """
    return token_re.fullmatch(token) is not None
