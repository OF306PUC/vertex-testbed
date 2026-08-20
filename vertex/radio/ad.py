"""BLE advertising data: element assembly and parsing.

AD (AdvData inside PDU payload) is a sequence of length-prefixed elements:

    len(1) | type(1) | value(len-1)

The length byte counts *type + value*, not the element's total size. A name
element occupies 9 bytes but its length byte is 8. 
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

__all__ = ["MAX_AD_LEN", "AD_FLAGS", "AD_NAME_COMPLETE", "AD_MANUFACTURER",
           "AdError", "Element", "element", "build_ad", "parse_ad",
           "manufacturer_value", "find_manufacturer"]

MAX_AD_LEN = 31             # max. payload is 37 bytes, but 6 are used to id the bluetooth address of the adv. device. 

AD_FLAGS = 0x01
AD_NAME_SHORT = 0x08
AD_NAME_COMPLETE = 0x09
AD_MANUFACTURER = 0xFF


class AdError(ValueError):
    """Malformed advertising data."""


@dataclass(frozen=True, slots=True)
class Element:
    type: int
    value: bytes


def element(ad_type: int, value: bytes) -> bytes:
    """One AD element. Length is computed, never written by hand."""
    if not 0 <= ad_type <= 0xFF:
        raise AdError(f"AD type must be a byte, got {ad_type}")
    if len(value) > 254:
        raise AdError(f"element value of {len(value)} bytes is too long")
    return bytes([len(value) + 1, ad_type]) + value


def build_ad(*elements: bytes, limit: int = MAX_AD_LEN) -> bytes:
    """Concatenate elements, refusing to exceed the advertising budget.
    """
    ad = b"".join(elements)
    if len(ad) > limit:
        raise AdError(f"advertising data is {len(ad)} bytes, limit is {limit}")
    return ad


def parse_ad(data: bytes, *, strict: bool = False) -> Iterator[Element]:
    """Walk AD elements.
    """
    i = 0
    n = len(data)
    while i < n:
        length = data[i]
        if length == 0:
            return                      # padding: end of significant data
        if i + 1 + length > n:
            if strict:
                raise AdError(
                    f"element at offset {i} declares {length} bytes, "
                    f"only {n - i - 1} remain")
            return
        yield Element(data[i + 1], data[i + 2:i + 1 + length])
        i += 1 + length


def manufacturer_value(company_id: int, payload: bytes) -> bytes:
    """Company ID (little-endian) followed by the payload."""
    if not 0 <= company_id <= 0xFFFF:
        raise AdError(f"company id must be uint16, got {company_id}")
    return company_id.to_bytes(2, "little") + payload


def find_manufacturer(data: bytes, company_id: int) -> bytes | None:
    """Payload of the first manufacturer element matching ``company_id``.
        :returns: payload of the first manufacturer element matching that company, 
                  or None. 
    """
    for el in parse_ad(data):
        if el.type == AD_MANUFACTURER and len(el.value) >= 2:
            if int.from_bytes(el.value[:2], "little") == company_id:
                return el.value[2:]
    return None


"""
Example: 
    ad = build_ad(
        element(AD_NAME_COMPLETE, b"LABCTRL"),
        element(AD_MANUFACTURER, manufacturer_value(0x0059, pkt.encode())),
    )
    payload = find_manufacturer(ad, 0x0059)      # None if it isn't ours
"""