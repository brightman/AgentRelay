import re
from typing import Iterable


AGENT_HRP = "agent"
TOPIC_OWNER_HRP = "topicowner"
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CHARSET_MAP = {c: i for i, c in enumerate(_CHARSET)}
_ADDRESS_RE = re.compile(r"^([^@\s]+)@([A-Za-z0-9.-]+)$")


def _bech32_polymod(values: Iterable[int]) -> int:
    chk = 1
    for value in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        if b & 1:
            chk ^= 0x3B6A57B2
        if b & 2:
            chk ^= 0x26508E6D
        if b & 4:
            chk ^= 0x1EA119FA
        if b & 8:
            chk ^= 0x3D4233DD
        if b & 16:
            chk ^= 0x2A1462B3
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_encode(hrp: str, data: list[int]) -> str:
    combined = data + _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(_CHARSET[d] for d in combined)


def _bech32_decode(value: str) -> tuple[str, list[int]]:
    if not value or value.lower() != value or value.upper() == value:
        raise ValueError("bech32 value must be lowercase")
    pos = value.rfind("1")
    if pos < 1 or pos + 7 > len(value):
        raise ValueError("invalid bech32 separator position")
    hrp = value[:pos]
    data = []
    for ch in value[pos + 1 :]:
        if ch not in _CHARSET_MAP:
            raise ValueError("invalid bech32 character")
        data.append(_CHARSET_MAP[ch])
    if not _bech32_verify_checksum(hrp, data):
        raise ValueError("invalid bech32 checksum")
    return hrp, data[:-6]


def _convertbits(data: Iterable[int], from_bits: int, to_bits: int, pad: bool) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << to_bits) - 1
    max_acc = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise ValueError("invalid value for convertbits")
        acc = ((acc << from_bits) | value) & max_acc
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        raise ValueError("invalid padding in convertbits")
    return ret


def is_hex_agent_id(value: str) -> bool:
    return bool(_HEX_RE.fullmatch(value or ""))


def encode_public_key_bech32(pubkey_hex: str, hrp: str = AGENT_HRP) -> str:
    if not is_hex_agent_id(pubkey_hex):
        raise ValueError("public key hex must be 32-byte hex")
    data = _convertbits(bytes.fromhex(pubkey_hex.lower()), 8, 5, True)
    return _bech32_encode(hrp, data)


def decode_public_key_bech32(value: str, expected_hrp: str = AGENT_HRP) -> str:
    hrp, data = _bech32_decode(value.strip())
    if hrp != expected_hrp:
        raise ValueError(f"unexpected bech32 hrp: {hrp}")
    decoded = bytes(_convertbits(data, 5, 8, False))
    if len(decoded) != 32:
        raise ValueError("decoded public key must be 32 bytes")
    return decoded.hex()


def normalize_agent_id(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("agent id is required")
    if is_hex_agent_id(candidate):
        return candidate.lower()
    return decode_public_key_bech32(candidate, expected_hrp=AGENT_HRP)


def format_agent_ref(pubkey_hex: str, relay_domain: str = "") -> dict[str, str]:
    agent_id = normalize_agent_id(pubkey_hex)
    agent_address = encode_public_key_bech32(agent_id)
    if relay_domain:
        agent_address = f"{agent_address}@{relay_domain}"
    return {
        "agent_id": agent_id,
        "agent_address": agent_address,
    }


def parse_agent_address(value: str) -> dict[str, str]:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("agent address is required")
    m = _ADDRESS_RE.fullmatch(candidate)
    if not m:
        raise ValueError("agent address must be <localpart>@<relay-domain>")
    localpart, relay_domain = m.group(1), m.group(2).lower()
    return {
        "agent_id": normalize_agent_id(localpart),
        "relay_domain": relay_domain,
        "agent_address": f"{localpart}@{relay_domain}",
    }
