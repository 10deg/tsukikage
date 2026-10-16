"""High dynamic range output: the ISO 21496-1 gain map, a.k.a. "Ultra HDR".

A JPEG is eight bits per channel, and a single one of them cannot hold both the
display rendition of a lunar scene and the scene-referred radiance behind it:
the earthshine-lit hemisphere and the sunlit limb differ by seven or eight
stops, and the tone curve that makes the first readable throws the second away.
The trick the phone camera world settled on -- written up as ISO 21496-1 and
shipped by Google and Adobe as "Ultra HDR" -- is to keep the ordinary SDR JPEG
exactly as it is and hide a *second*, low-resolution grayscale JPEG inside the
same file.  That second image is the gain map: one number per pixel saying how
much brighter the HDR rendition is than the SDR one.  A viewer that knows
nothing about any of this sees a perfectly ordinary JPEG; a viewer that does
reads the map and multiplies.

Why it is a log2 ratio and not a multiplier
-------------------------------------------
A gain map is stored in eight bits.  A linear multiplier would spend almost all
of those bits on the values just above 1 and leave the bright end with almost
none -- exactly backwards, because the eye is roughly logarithmic and the
information worth keeping lives at the bright end.  So the stored quantity is
``log2(HDR / SDR)``, which spends the bits evenly across stops.  Two small
``OffsetSDR``/``OffsetHDR`` constants keep the ratio finite where the SDR pixel
is black, since ``log2(0/0)`` is not a number anyone can render.

Why ``sdr`` is decoded to linear first
--------------------------------------
The SDR rendition is an sRGB-encoded *display* image; the HDR rendition is
scene-referred *linear* light.  Comparing them means putting both in the same
space, so :func:`gain_map` runs the SDR through the sRGB transfer function
before taking the Rec.709 luminance of either.  That is also what makes
:func:`apply_gain_map` the exact inverse of :func:`gain_map`, which is the
property the numerics tests pin down.  The Rec.709 weights are used because the
SDR rendition is an sRGB product and those are its own luma weights.

Applying the gain map
---------------------
The decoder is the classical ISO 21496-1 form::

    hdr = (srgb_to_linear(sdr) + OffsetSDR) * 2**log2 - OffsetHDR

Setting both offsets to zero reduces this to the plain
``srgb_to_linear(sdr) * 2**log2``.  The offsets are carried through on *both*
sides on purpose: they are what makes the encode/decode pair exactly
invertible, and they are what a conforming viewer assumes.  :func:`gain_map`
computes the ratio the same way, so a pair round-trips to within one JPEG code
value wherever the gain is inside the clamped range.

The file layout
---------------
JPEG segment ordering is the contract a viewer relies on::

    SOI
    APP1   XMP, hdrgm: fields          (namespace .../hdr-gain-map/1.0/)
    APP2   MPF, "MPF\\0" + MP Index IFD (points at the gain map JPEG)
    ...    the main JPEG's own APPn/DQT/SOF/SOS/entropy data
    EOI
    SOI .. EOI                         (the gain map JPEG, appended verbatim)

The MPF entry's data offset is **relative to the MPF TIFF header**, i.e. to the
byte just past the ``"MPF\\0"`` signature, which is what CIPA DC-007 §5.2.3 and
Google's own reader (Skia's ``SkJpegMultiPictureParameters``) require.  The
summary returned by :func:`write_gain_map_jpeg` reports the *absolute* offset
from the file's SOI as ``gain_offset`` and the raw field as
``gain_offset_relative``, so a caller can check either one.

The quantisation convention
---------------------------
The encoded value in ``[0, 1]`` is recovered with::

    log2 = GainMapMin + (GainMapMax - GainMapMin) * encoded ** Gamma

so ``Gamma`` is the exponent applied to the encoded value on the way back, and
the encoder is its exact inverse::

    encoded = ((log2 - GainMapMin) / (GainMapMax - GainMapMin)) ** (1 / Gamma)

``Gamma == 0`` is treated as the identity curve (no exponent at all).  The
default of ``1.0`` is what a plain linear quantiser means.

Pillow is imported lazily, inside the two functions that need it, so that
``moonlib_s`` keeps working on an installation without an image codec; nothing
else in this module touches an image library.
"""

from __future__ import annotations

import io
import math
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from .errors import MoonlibError

__all__ = ["GainMap", "GainMapFile", "gain_map", "gain_map_metadata",
           "write_gain_map_jpeg", "write_gainmap_jpeg", "read_gainmap_jpeg",
           "decode_gain_map", "apply_gain_map", "sdr_hdr_pair"]


#: XMP namespace of the gain map metadata (ISO 21496-1 / Ultra HDR).
HDRGM_NAMESPACE = "http://ns.adobe.com/hdr-gain-map/1.0/"
#: The XMP packet's own namespace and the RDF namespace the fields live in.
XMP_NAMESPACE = "adobe:ns:meta/"
RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

#: The APP1 header every XMP packet in a JPEG carries, NUL included.
XMP_APP1_SIGNATURE = b"http://ns.adobe.com/xap/1.0/\x00"
#: The APP2 header of an MPF segment, per CIPA DC-007 §5.2.1.
MPF_APP2_SIGNATURE = b"MPF\x00"

#: Field order of the ``hdrgm:`` block, i.e. the order they are written in.
HDRGM_FIELDS: Tuple[str, ...] = (
    "Version", "BaseRenditionIsHDR", "GainMapMin", "GainMapMax", "Gamma",
    "OffsetSDR", "OffsetHDR", "HDRCapacityMin", "HDRCapacityMax",
)

#: MP Index IFD tags (CIPA DC-007 Table 3).
_MPF_TAG_VERSION = 0xB000
_MPF_TAG_NUMBER_OF_IMAGES = 0xB001
_MPF_TAG_MP_ENTRY = 0xB002
#: TIFF type codes we use: UNDEFINED and LONG.
_TIFF_UNDEFINED = 7
_TIFF_LONG = 4
#: MP Entry attribute: JPEG format (bits 26-24 zero) plus the primary type code.
#: Skia's reader keys the primary image off the type code, not off bit 30.
_MPF_ATTRIBUTE_PRIMARY = 0x030000
_MPF_ATTRIBUTE_AUXILIARY = 0x000000

#: Rec.709 luma weights: the SDR rendition is an sRGB product, so these are its
#: own weights rather than the 601 pair some other colour spaces would use.
_REC709 = (0.2126, 0.7152, 0.0722)


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------
def _srgb_to_linear(values: np.ndarray) -> np.ndarray:
    """IEC 61966-2-1 electro-optical transfer function (sRGB -> linear).

    Evaluated in double precision because the 2.4 exponent amplifies float32
    rounding enough to show up in a bit-exact comparison.  Values above 1 are
    not clamped here; that is the caller's decision.
    """
    data = np.asarray(values, dtype=np.float64)
    low = data / 12.92
    high = np.power((np.maximum(data, 0.0) + 0.055) / 1.055, 2.4)
    return np.where(data <= 0.04045, low, high)


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    """The exact inverse of :func:`_srgb_to_linear` (linear -> sRGB).

    Kept alongside the forward transform so that a caller who wants to *show*
    an HDR rendition -- or to build the SDR rendition from linear light in the
    first place -- does not have to reach for a second implementation.
    """
    data = np.asarray(values, dtype=np.float64)
    low = data * 12.92
    high = 1.055 * np.power(np.maximum(data, 0.0), 1.0 / 2.4) - 0.055
    return np.where(data <= 0.0031308, low, high)


def _luminance(values: np.ndarray) -> np.ndarray:
    """Rec.709 luma of a ``(h, w)`` or ``(h, w, 3)`` array.

    A single-channel array is already a luminance, so it is returned as is.
    """
    data = np.asarray(values, dtype=np.float64)
    if data.ndim == 2:
        return data
    weights = np.asarray(_REC709, dtype=np.float64)
    return data[..., 0] * weights[0] + data[..., 1] * weights[1] + data[..., 2] * weights[2]


def _finite_float(value: Any, name: str) -> float:
    """A caller-supplied scalar as a finite float, or :class:`MoonlibError`."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MoonlibError(f"{name} must be a number, got {value!r}.") from exc
    if not math.isfinite(number):
        raise MoonlibError(f"{name} must be finite, got {number!r}.")
    return number


def _image_array(values: Any, name: str) -> np.ndarray:
    """Validate one SDR/HDR picture: ``(h, w)`` or ``(h, w, 3)``, finite.

    The shape is enforced rather than repaired because a silently broadcast
    wrong-shaped image is exactly the bug a gain map makes hard to see: the
    picture still comes out, just with somebody else's brightness in it.
    """
    data = np.asarray(values, dtype=np.float32)
    if data.ndim not in (2, 3):
        raise MoonlibError(
            f"{name} must be (height, width) or (height, width, 3), "
            f"got shape {data.shape}."
        )
    if data.ndim == 3 and data.shape[2] != 3:
        raise MoonlibError(
            f"{name} must have exactly 3 channels, got shape {data.shape}."
        )
    if data.size == 0 or data.shape[0] == 0 or data.shape[1] == 0:
        raise MoonlibError(f"{name} is empty (shape {data.shape}).")
    if not np.all(np.isfinite(data)):
        raise MoonlibError(f"{name} contains NaN or infinity.")
    return data


def _gain_parameters(metadata: Any) -> Dict[str, float]:
    """Read the six numbers that define the transfer curve out of *metadata*.

    Accepts a :class:`GainMap`, the dict :meth:`GainMap.to_dict` produces (XMP
    field names) or the same dict written with the dataclass's own field names.
    Anything else is a caller mistake and raises :class:`MoonlibError`.
    """
    if isinstance(metadata, GainMap):
        return {
            "min_log2": float(metadata.min_log2),
            "max_log2": float(metadata.max_log2),
            "gamma": float(metadata.gamma),
            "offset_sdr": float(metadata.offset_sdr),
            "offset_hdr": float(metadata.offset_hdr),
            "capacity_min": float(metadata.capacity_min),
            "capacity_max": float(metadata.capacity_max),
        }
    if not isinstance(metadata, dict):
        raise MoonlibError(
            "metadata must be a GainMap or a mapping of its fields, got "
            f"{type(metadata).__name__}."
        )

    # XMP names first, then the dataclass spelling.  Gamma, the offsets and the
    # capacities have documented defaults; the two ends of the range do not,
    # because guessing them would silently change every recovered value.
    aliases = {
        "min_log2": ("GainMapMin", "min_log2"),
        "max_log2": ("GainMapMax", "max_log2"),
        "gamma": ("Gamma", "gamma"),
        "offset_sdr": ("OffsetSDR", "offset_sdr"),
        "offset_hdr": ("OffsetHDR", "offset_hdr"),
        "capacity_min": ("HDRCapacityMin", "capacity_min"),
        "capacity_max": ("HDRCapacityMax", "capacity_max"),
    }
    defaults = {"gamma": 1.0, "offset_sdr": 0.0, "offset_hdr": 0.0}
    found: Dict[str, float] = {}
    for field, names in aliases.items():
        for name in names:
            if name in metadata and metadata[name] is not None:
                found[field] = _finite_float(metadata[name], f"metadata[{name!r}]")
                break
    for required in ("min_log2", "max_log2"):
        if required not in found:
            raise MoonlibError(
                f"metadata is missing {aliases[required][0]!r}; a gain map "
                "cannot be decoded without both ends of its range."
            )
    for field, default in defaults.items():
        found.setdefault(field, default)
    found.setdefault("capacity_min", found["min_log2"])
    found.setdefault("capacity_max", found["max_log2"])
    if found["max_log2"] <= found["min_log2"]:
        raise MoonlibError(
            f"metadata GainMapMax ({found['max_log2']}) must be greater than "
            f"GainMapMin ({found['min_log2']})."
        )
    return found


# ---------------------------------------------------------------------------
# the gain map itself
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GainMap:
    """The encoded gain map plus everything needed to decode it.

    ``data`` is the *encoded* map actually stored in the JPEG: ``float32`` of
    shape ``(height, width)`` in ``[0, 1]``, where 0 means ``min_log2`` and 1
    means ``max_log2``.  It is not a multiplier and not a radiance; pass it to
    :func:`decode_gain_map` or straight to :func:`apply_gain_map`.
    """

    data: np.ndarray
    #: GainMapMin / GainMapMax: the log2 range the encoding spans, in stops.
    min_log2: float
    max_log2: float
    #: Exponent the decoder applies to the encoded value (see module docstring).
    gamma: float
    #: Constants that keep the ratio finite where the SDR pixel is black.
    offset_sdr: float
    offset_hdr: float
    #: HDRCapacityMin / HDRCapacityMax: the HDR headroom the map was built for.
    capacity_min: float
    capacity_max: float

    def to_dict(self) -> Dict[str, Any]:
        """The JSON-safe metadata block, with the XMP ``hdrgm:`` field names."""
        return gain_map_metadata(self)


@dataclass(frozen=True)
class GainMapFile:
    """One gain-map JPEG, taken apart into the pieces a caller works with."""

    #: The primary (SDR) JPEG stream, SOI through the matching EOI.
    main_jpeg: bytes
    #: The auxiliary (gain map) JPEG stream, SOI through the matching EOI.
    gain_jpeg: bytes
    #: The gain map decoded out of *gain_jpeg*, with its metadata.
    gain_map: GainMap
    #: The raw ``hdrgm:`` fields as read from the file (strings, as XMP has them).
    metadata: Dict[str, Any]
    #: Absolute byte offset of the gain map's SOI, counted from the file's SOI.
    gain_offset: int
    #: Length of the gain map JPEG in bytes.
    gain_length: int


def gain_map(
    sdr: Any,
    hdr: Any,
    *,
    stops: float = 3.0,
    gamma: float = 1.0,
    offset_sdr: float = 1.0 / 64.0,
    offset_hdr: float = 1.0 / 64.0,
    capacity_min: float = 0.0,
    capacity_max: Optional[float] = None,
) -> GainMap:
    """Measure the gain map that turns an SDR rendition into an HDR one.

    Parameters
    ----------
    sdr, hdr
        ``(height, width)`` or ``(height, width, 3)`` float arrays of the same
        shape.  *sdr* is the display rendition, i.e. sRGB-encoded and nominally
        in ``[0, 1]`` (it is clipped there before the transfer function);
        *hdr* is the scene-referred linear rendition of the same picture and
        may exceed 1 wherever the tone curve compressed a highlight.
    stops
        Default width of the log2 range, i.e. the default ``capacity_max``.
    gamma
        Encoder exponent.  ``encoded = t ** (1 / gamma)`` for ``gamma != 0``,
        with ``t`` the ratio linearly mapped into the range; ``gamma == 0``
        means the identity.  See the module docstring for the decoder.
    offset_sdr, offset_hdr
        The ISO 21496-1 regularisers, added to the SDR luminance and carried on
        the other side when the map is applied.  They must be non-negative, and
        ``offset_sdr`` must be positive if any SDR pixel is black.
    capacity_min, capacity_max
        The log2 range the encoding spans.  ``capacity_max`` defaults to
        *stops*.

    Returns
    -------
    GainMap
        ``min_log2`` and ``max_log2`` are the GainMapMin / GainMapMax actually
        used, i.e. the clamp bounds, not the extremes of the raw ratio.

    Raises
    ------
    MoonlibError
        If a shape, a scalar or a pixel is something the caller got wrong: a
        mismatch between *sdr* and *hdr*, a non-finite sample, a non-positive
        range, or a negative offset.

    Notes
    -----
    The ratio is ``log2((hdr_lum + offset_hdr) / (sdr_lum + offset_sdr))`` on
    Rec.709 luminances, with *sdr* first decoded through the sRGB transfer
    function.  Every sample is then clamped into
    ``[capacity_min, capacity_max]``: a scene with more than *stops* of
    headroom loses the excess, and the clamp is reported through the returned
    ``min_log2``/``max_log2`` rather than hidden.
    """
    stops_value = _finite_float(stops, "stops")
    if stops_value <= 0.0:
        raise MoonlibError(f"stops must be positive, got {stops_value!r}.")
    gamma_value = _finite_float(gamma, "gamma")
    if gamma_value < 0.0:
        raise MoonlibError(f"gamma must not be negative, got {gamma_value!r}.")
    low_offset = _finite_float(offset_sdr, "offset_sdr")
    high_offset = _finite_float(offset_hdr, "offset_hdr")
    if low_offset < 0.0 or high_offset < 0.0:
        raise MoonlibError("offset_sdr and offset_hdr must not be negative.")

    low = _finite_float(capacity_min, "capacity_min")
    high = stops_value if capacity_max is None else _finite_float(
        capacity_max, "capacity_max")
    if high <= low:
        raise MoonlibError(
            f"capacity_max ({high}) must be greater than capacity_min ({low})."
        )

    sdr_values = _image_array(sdr, "sdr")
    hdr_values = _image_array(hdr, "hdr")
    if sdr_values.shape != hdr_values.shape:
        raise MoonlibError(
            f"sdr and hdr must have the same shape, got {sdr_values.shape} "
            f"and {hdr_values.shape}."
        )

    # Both sides in linear light, then Rec.709 luma.  The SDR display image is
    # clamped first: a display value outside [0, 1] is not something the
    # transfer function is defined for.
    sdr_linear = _srgb_to_linear(np.clip(sdr_values.astype(np.float64), 0.0, 1.0))
    sdr_lum = _luminance(sdr_linear)
    hdr_lum = _luminance(hdr_values)

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.log2((hdr_lum + high_offset) / (sdr_lum + low_offset))
    if not np.all(np.isfinite(ratio)):
        raise MoonlibError(
            "the gain ratio is not finite: check offset_sdr (it must be "
            "positive if the SDR picture has black pixels)."
        )

    clamped = np.clip(ratio, low, high)
    span = high - low
    scaled = (clamped - low) / span
    if gamma_value != 0.0:
        encoded = np.power(scaled, 1.0 / gamma_value)
    else:
        encoded = scaled
    # np.power on a clamped [0, 1] base cannot leave the interval for a
    # positive exponent; clip anyway so a gamma of 0 or a future change here
    # cannot hand a caller an out-of-range code value.
    encoded = np.clip(encoded, 0.0, 1.0).astype(np.float32)

    return GainMap(
        data=encoded,
        min_log2=float(low),
        max_log2=float(high),
        gamma=float(gamma_value),
        offset_sdr=float(low_offset),
        offset_hdr=float(high_offset),
        capacity_min=float(low),
        capacity_max=float(high),
    )


def _format_number(value: float) -> str:
    """A float as the string XMP wants for one of these fields."""
    return "{:.10g}".format(float(value))


def gain_map_metadata(gain: GainMap) -> Dict[str, Any]:
    """The ``hdrgm:`` XMP attributes of *gain*, every value a string.

    XMP stores these as simple values of string type, so the numbers are
    formatted rather than handed over as floats; ten significant digits is more
    than an eight-bit gain map can use and keeps ``1/64`` exact.
    """
    return {
        "Version": "1.0",
        "BaseRenditionIsHDR": "False",
        "GainMapMin": _format_number(gain.min_log2),
        "GainMapMax": _format_number(gain.max_log2),
        "Gamma": _format_number(gain.gamma),
        "OffsetSDR": _format_number(gain.offset_sdr),
        "OffsetHDR": _format_number(gain.offset_hdr),
        "HDRCapacityMin": _format_number(gain.capacity_min),
        "HDRCapacityMax": _format_number(gain.capacity_max),
    }


# ---------------------------------------------------------------------------
# decode / apply
# ---------------------------------------------------------------------------
def decode_gain_map(data: Any, metadata: Any) -> np.ndarray:
    """Invert the encoder: encoded ``[0, 1]`` values -> log2 gain in stops.

    Parameters
    ----------
    data
        The encoded map, ``(height, width)``, as stored in the gain JPEG.
    metadata
        A :class:`GainMap`, the dict from :meth:`GainMap.to_dict`, or an
        XMP-style mapping with ``GainMapMin`` / ``GainMapMax`` / ``Gamma``.

    Returns
    -------
    numpy.ndarray
        ``float32`` log2 gains, the same shape as *data*.  Multiply
        ``srgb_to_linear(sdr)`` by ``2 ** this`` to recover the HDR rendition.
    """
    parameters = _gain_parameters(metadata)
    encoded = np.asarray(data, dtype=np.float64)
    if not np.all(np.isfinite(encoded)):
        raise MoonlibError("the encoded gain map contains NaN or infinity.")
    encoded = np.clip(encoded, 0.0, 1.0)
    gamma = parameters["gamma"]
    curved = np.power(encoded, gamma) if gamma != 0.0 else encoded
    log2 = parameters["min_log2"] + (
        parameters["max_log2"] - parameters["min_log2"]) * curved
    return log2.astype(np.float32)


def apply_gain_map(sdr: Any, data: Any, metadata: Any) -> np.ndarray:
    """Apply a gain map to the SDR rendition, recovering an HDR rendition.

    Parameters
    ----------
    sdr
        The SDR display rendition, ``(height, width)`` or ``(height, width, 3)``.
    data
        The encoded gain map, ``(height, width)``.
    metadata
        A :class:`GainMap`, its ``to_dict()``, or an XMP-style mapping.

    Returns
    -------
    numpy.ndarray
        ``float32`` linear-light HDR radiance, the shape of *sdr*, computed as
        ``(srgb_to_linear(sdr) + OffsetSDR) * 2**log2 - OffsetHDR``.  With both
        offsets zero this is the plain ``srgb_to_linear(sdr) * 2**log2``; the
        offsets are what make it the exact inverse of :func:`gain_map`.  The
        result is clipped at zero, since a negative radiance is not a picture.

    Raises
    ------
    MoonlibError
        For a mismatched shape or metadata that does not define a range.
    """
    display = _image_array(sdr, "sdr")
    parameters = _gain_parameters(metadata)
    log2 = decode_gain_map(data, parameters)
    if log2.shape != display.shape[:2]:
        raise MoonlibError(
            f"the gain map is {log2.shape} but sdr is {display.shape}; they "
            "must cover the same picture."
        )
    linear = _srgb_to_linear(np.clip(display.astype(np.float64), 0.0, 1.0))
    gains = np.exp2(log2.astype(np.float64))
    if linear.ndim == 3:
        gains = gains[:, :, None]
    hdr = (linear + parameters["offset_sdr"]) * gains - parameters["offset_hdr"]
    return np.clip(hdr, 0.0, None).astype(np.float32)


# ---------------------------------------------------------------------------
# JPEG / XMP / MPF byte assembly
# ---------------------------------------------------------------------------
def _pillow_image() -> Any:
    """Import Pillow, or explain what to install.

    Pillow is only needed for the two ends of this module: turning the float
    arrays into JPEG code streams and turning them back.  It arrives for free
    with ``moonlib_s[plot]`` because matplotlib depends on it, and with
    ``moonlib_s[tiff]``.
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise MoonlibError(
            "HDR gain-map JPEG output needs Pillow to encode and decode the "
            "two JPEG streams. Install it with: pip install 'moonlib_s[plot]' "
            "or pip install Pillow."
        ) from exc
    return Image


def _to_uint8(values: np.ndarray) -> np.ndarray:
    """Quantise ``[0, 1]`` float values to 8-bit codes, rounding to nearest."""
    scaled = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0) * 255.0
    return (scaled + 0.5).astype(np.uint8)


def _encode_jpeg(values: np.ndarray, quality: int) -> bytes:
    """One image as a complete JPEG stream (SOI .. EOI)."""
    Image = _pillow_image()
    if not 1 <= int(quality) <= 100:
        raise MoonlibError(f"quality must be in 1..100, got {quality!r}.")
    codes = _to_uint8(values)
    # Pillow infers the mode from the array: (h, w) uint8 is "L", (h, w, 3) is
    # "RGB".  Passing `mode=` explicitly is deprecated as of Pillow 11.3.
    image = Image.fromarray(codes)
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="JPEG", quality=int(quality))
    except Exception as exc:  # pragma: no cover - Pillow's own encoder errors
        raise MoonlibError(f"Pillow could not encode the JPEG: {exc}") from exc
    return buffer.getvalue()


def _xml_escape(value: str) -> str:
    """Escape one XML attribute value."""
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xmp_packet(fields: Dict[str, Any]) -> bytes:
    """Build the XMP packet that carries the ``hdrgm:`` block."""
    attributes = "\n".join(
        '    hdrgm:{0}="{1}"'.format(name, _xml_escape(str(fields[name])))
        for name in HDRGM_FIELDS if name in fields
    )
    packet = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="moonlib_s">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:hdrgm="{0}"\n'
        '{1}/>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    ).format(HDRGM_NAMESPACE, attributes)
    return packet.encode("utf-8")


def _xmp_segment(fields: Dict[str, Any]) -> bytes:
    """The APP1 segment (marker, length, signature, packet) for *fields*."""
    payload = XMP_APP1_SIGNATURE + _xmp_packet(fields)
    if len(payload) + 2 > 0xFFFF:
        raise MoonlibError(
            "the XMP metadata does not fit in one APP1 segment; reduce the "
            "number of gain map fields."
        )
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def _ifd_entry(tag: int, type_id: int, count: int, value: bytes) -> bytes:
    """One 12-byte TIFF IFD entry with its value inline (must be 4 bytes)."""
    if len(value) != 4:  # pragma: no cover - internal invariant
        raise MoonlibError("an inline TIFF value must be exactly 4 bytes.")
    return struct.pack(">HHI", tag, type_id, count) + value


def _mp_entry(attribute: int, size: int, offset: int) -> bytes:
    """One 16-byte MP Entry: attribute, size, offset, two dependent indices."""
    return struct.pack(">IIIHH", attribute, size, offset, 0, 0)


def _mpf_segment(primary_size: int, gain_offset_relative: int,
                 gain_length: int) -> bytes:
    """The APP2 MPF segment for a primary image plus one appended gain map.

    The layout follows Skia's serialiser: big-endian TIFF, the MP Index IFD at
    offset 8, three tags in ascending order, and the 32-byte MP Entry array
    after the next-IFD pointer -- so the array sits at a fixed offset of 50
    bytes into the TIFF data, which is what the value field of tag 0xB002
    records.

    *gain_offset_relative* is counted from the start of the TIFF header, i.e.
    from the byte after the ``"MPF\\0"`` signature, as CIPA DC-007 §5.2.3
    requires.  *primary_size* is everything from the file's SOI up to the gain
    map's SOI.
    """
    tiff = bytearray()
    tiff += b"MM"                              # big-endian, like Skia writes
    tiff += struct.pack(">H", 0x002A)          # the TIFF magic
    tiff += struct.pack(">I", 8)               # MP Index IFD starts at 8
    tiff += struct.pack(">H", 3)               # three IFD entries
    tiff += _ifd_entry(_MPF_TAG_VERSION, _TIFF_UNDEFINED, 4, b"0100")
    tiff += _ifd_entry(_MPF_TAG_NUMBER_OF_IMAGES, _TIFF_LONG, 1,
                       struct.pack(">I", 2))
    # The MP Entry array follows this entry and the 4-byte next-IFD pointer.
    entries_offset = len(tiff) + 12 + 4
    tiff += _ifd_entry(_MPF_TAG_MP_ENTRY, _TIFF_UNDEFINED, 32,
                       struct.pack(">I", entries_offset))
    tiff += struct.pack(">I", 0)               # no MP Attribute IFD
    if len(tiff) != entries_offset:  # pragma: no cover - internal invariant
        raise MoonlibError("the MPF index IFD is not the size it should be.")
    tiff += _mp_entry(_MPF_ATTRIBUTE_PRIMARY, primary_size, 0)
    tiff += _mp_entry(_MPF_ATTRIBUTE_AUXILIARY, gain_length, gain_offset_relative)

    payload = MPF_APP2_SIGNATURE + bytes(tiff)
    return b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload


def _normalise_override(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A caller's metadata override as XMP field strings."""
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise MoonlibError(
            "metadata must be a mapping of hdrgm: fields, got "
            f"{type(metadata).__name__}."
        )
    field_names = {
        "min_log2": "GainMapMin", "max_log2": "GainMapMax", "gamma": "Gamma",
        "offset_sdr": "OffsetSDR", "offset_hdr": "OffsetHDR",
        "capacity_min": "HDRCapacityMin", "capacity_max": "HDRCapacityMax",
    }
    normalised: Dict[str, Any] = {}
    for key, value in metadata.items():
        name = field_names.get(key, key)
        if name not in HDRGM_FIELDS:
            raise MoonlibError(
                f"unknown gain map metadata field {key!r}; known fields: "
                f"{', '.join(HDRGM_FIELDS)}."
            )
        if name in ("Version", "BaseRenditionIsHDR"):
            normalised[name] = str(value)
        else:
            normalised[name] = _format_number(_finite_float(value, name))
    return normalised


def write_gain_map_jpeg(
    path: Union[str, Path],
    sdr: Any,
    gain: GainMap,
    *,
    quality: int = 95,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write an SDR JPEG and its gain map as one ISO 21496-1 file.

    The two JPEGs are encoded separately and then spliced together by hand,
    because Pillow has no MPF API and no way to append an auxiliary image with
    the offsets a conforming viewer needs.  The APP1 XMP and APP2 MPF segments
    are inserted immediately after the main image's SOI, so the main stream
    stays a valid JPEG from byte 0 and the gain stream follows its EOI.

    Parameters
    ----------
    path
        Where to write the file.
    sdr
        The main picture: ``(height, width, 3)`` float in ``[0, 1]``, already
        display-encoded (clipped, not re-encoded).
    gain
        The map from :func:`gain_map`, whose ``data`` must match *sdr*'s frame.
    quality
        libjpeg quality for both streams.  The gain map wants a high one: it is
        a ratio field, and JPEG ringing in it shows as banding in the recovered
        highlights.
    metadata
        Optional override of the ``hdrgm:`` fields, keyed either by XMP name or
        by :class:`GainMap` field name.  Values default to *gain*'s own.

    Returns
    -------
    dict
        ``path``, ``bytes``, ``sdr_offset`` (always 0 -- the main SOI is the
        file's first byte), ``gain_offset`` (absolute, from that SOI),
        ``gain_length``, ``metadata`` (what was actually written), and
        ``gain_offset_relative`` / ``mpf_tiff_offset`` for callers that want to
        check the raw MPF field.

    Raises
    ------
    MoonlibError
        For a wrong-shaped picture, a gain map that does not match it, a bad
        quality, an unknown metadata field, or an unwritable path.
    """
    if not isinstance(gain, GainMap):
        raise MoonlibError(
            f"gain must be a GainMap, got {type(gain).__name__}."
        )
    display = _image_array(sdr, "sdr")
    if display.ndim != 3:
        raise MoonlibError(
            "the SDR main image must be (height, width, 3) for a JPEG; got "
            f"shape {display.shape}."
        )
    encoded = np.asarray(gain.data, dtype=np.float32)
    if encoded.ndim != 2:
        raise MoonlibError(
            f"gain.data must be (height, width), got shape {encoded.shape}."
        )
    if encoded.shape != display.shape[:2]:
        raise MoonlibError(
            f"gain.data is {encoded.shape} but sdr is {display.shape[:2]}; "
            "the map must cover the main picture."
        )
    if not np.all(np.isfinite(encoded)):
        raise MoonlibError("gain.data contains NaN or infinity.")
    if not 1 <= int(quality) <= 100:
        raise MoonlibError(f"quality must be in 1..100, got {quality!r}.")

    fields: Dict[str, Any] = dict(gain.to_dict())
    fields.update(_normalise_override(metadata))

    main_jpeg = _encode_jpeg(display, int(quality))
    gain_jpeg = _encode_jpeg(encoded, int(quality))
    xmp_segment = _xmp_segment(fields)

    # The MPF segment's own length does not depend on the values it carries, so
    # one throwaway build is enough to know where the gain map will land.
    insertion = 2  # immediately after the main image's SOI
    mpf_tiff_offset = insertion + len(xmp_segment) + 4 + len(MPF_APP2_SIGNATURE)
    proxy = _mpf_segment(0, 0, 0)
    gain_offset = (insertion + len(xmp_segment) + len(proxy)
                   + (len(main_jpeg) - insertion))
    gain_length = len(gain_jpeg)
    gain_offset_relative = gain_offset - mpf_tiff_offset
    mpf_segment = _mpf_segment(gain_offset, gain_offset_relative, gain_length)
    if len(mpf_segment) != len(proxy):  # pragma: no cover - internal invariant
        raise MoonlibError("the MPF segment changed size while being built.")

    blob = (main_jpeg[:insertion] + xmp_segment + mpf_segment
            + main_jpeg[insertion:] + gain_jpeg)
    if len(blob) != gain_offset + gain_length:  # pragma: no cover - invariant
        raise MoonlibError("the assembled JPEG is not the length it should be.")

    destination = Path(path)
    try:
        destination.write_bytes(blob)
    except OSError as exc:
        raise MoonlibError(f"cannot write {destination}: {exc}") from exc

    return {
        "path": str(destination),
        "bytes": len(blob),
        "sdr_offset": 0,
        "gain_offset": gain_offset,
        "gain_length": gain_length,
        "metadata": fields,
        "gain_offset_relative": gain_offset_relative,
        "mpf_tiff_offset": mpf_tiff_offset,
    }


def write_gainmap_jpeg(
    path: Union[str, Path],
    sdr: Any,
    hdr: Any,
    *,
    quality: int = 95,
    stops: float = 3.0,
    gamma: float = 1.0,
) -> Dict[str, Any]:
    """The one-call convenience: :func:`gain_map` then write the file.

    Returns the same summary as :func:`write_gain_map_jpeg`.
    """
    measured = gain_map(sdr, hdr, stops=stops, gamma=gamma)
    return write_gain_map_jpeg(path, sdr, measured, quality=quality)


# ---------------------------------------------------------------------------
# reading the file back
# ---------------------------------------------------------------------------
def _scan_jpeg(data: bytes, start: int) -> Tuple[int, List[Tuple[int, int, int]]]:
    """One JPEG stream's end offset and its header segments.

    Segments are ``(marker, payload_start, payload_end)``.  Walking them by
    their declared lengths rather than searching for ``FFD9`` matters: an
    APP1/APP2 payload is arbitrary bytes and may legitimately contain that pair,
    which a naive scan would mistake for the end of the image.
    """
    if data[start:start + 2] != b"\xff\xd8":
        raise MoonlibError(f"no JPEG SOI at offset {start}.")
    pos = start + 2
    segments: List[Tuple[int, int, int]] = []
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xD9:  # EOI
            return pos + 2, segments
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2  # a standalone marker, no payload
            continue
        if pos + 4 > len(data):
            break
        length = (data[pos + 2] << 8) | data[pos + 3]
        if length < 2:
            break
        payload_start = pos + 4
        payload_end = pos + 2 + length
        if payload_end > len(data):
            break
        segments.append((marker, payload_start, payload_end))
        if marker == 0xDA:  # SOS: entropy-coded data runs to the next EOI
            pos = payload_end
            while pos + 1 < len(data):
                if data[pos] == 0xFF:
                    following = data[pos + 1]
                    if following == 0xD9:
                        return pos + 2, segments
                    if following == 0x00 or 0xD0 <= following <= 0xD7:
                        pos += 2
                        continue
                    if pos + 4 > len(data):
                        break
                    length = (data[pos + 2] << 8) | data[pos + 3]
                    pos += 2 + max(length, 2)
                    continue
                pos += 1
            return len(data), segments
        pos = payload_end
    return pos, segments


def _jpeg_streams(data: bytes) -> List[Tuple[int, int]]:
    """Every JPEG stream in *data*, as ``(start, end)`` byte ranges."""
    streams: List[Tuple[int, int]] = []
    pos = 0
    while pos + 1 < len(data):
        found = data.find(b"\xff\xd8", pos)
        if found < 0:
            break
        end, _segments = _scan_jpeg(data, found)
        streams.append((found, end))
        pos = max(end, found + 2)
    return streams


def _read_u16(data: bytes, offset: int, big_endian: bool) -> int:
    return int.from_bytes(data[offset:offset + 2],
                          "big" if big_endian else "little")


def _read_u32(data: bytes, offset: int, big_endian: bool) -> int:
    return int.from_bytes(data[offset:offset + 4],
                          "big" if big_endian else "little")


def _parse_mpf(data: bytes, payload_start: int,
               payload_end: int) -> Optional[Tuple[int, int]]:
    """``(absolute_offset, length)`` of the auxiliary image an MPF points at.

    ``None`` when the segment is not a usable MPF.  The stored offset is
    relative to the TIFF header -- the byte after ``"MPF\\0"`` -- which is the
    position Skia and CIPA DC-007 use.
    """
    payload = data[payload_start:payload_end]
    if not payload.startswith(MPF_APP2_SIGNATURE):
        return None
    tiff_start = payload_start + len(MPF_APP2_SIGNATURE)
    tiff = payload[len(MPF_APP2_SIGNATURE):]
    if len(tiff) < 8 or tiff[0:2] not in (b"MM", b"II"):
        return None
    big_endian = tiff[0:2] == b"MM"
    ifd_offset = _read_u32(tiff, 4, big_endian)
    if ifd_offset + 2 > len(tiff):
        return None
    entries = _read_u16(tiff, ifd_offset, big_endian)
    entries_offset = 0
    for index in range(entries):
        entry = ifd_offset + 2 + index * 12
        if entry + 12 > len(tiff):
            return None
        tag = _read_u16(tiff, entry, big_endian)
        if tag == _MPF_TAG_MP_ENTRY:
            entries_offset = _read_u32(tiff, entry + 8, big_endian)
    if entries_offset == 0 or entries_offset + 32 > len(tiff):
        return None
    # Entry 0 is the primary image (offset 0 by definition); entry 1 is the map.
    offset = _read_u32(tiff, entries_offset + 16 + 8, big_endian)
    length = _read_u32(tiff, entries_offset + 16 + 4, big_endian)
    if offset == 0 or length == 0:
        return None
    return tiff_start + offset, length


def _parse_xmp(payload: bytes) -> Optional[Dict[str, str]]:
    """The ``hdrgm:`` fields of one XMP APP1 payload, or ``None``."""
    if not payload.startswith(XMP_APP1_SIGNATURE):
        return None
    text = payload[len(XMP_APP1_SIGNATURE):].decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    fields: Dict[str, str] = {}
    prefix = "{" + HDRGM_NAMESPACE + "}"
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.startswith(prefix):
                fields[key[len(prefix):]] = value
        if isinstance(element.tag, str) and element.tag.startswith(prefix):
            fields[element.tag[len(prefix):]] = (element.text or "").strip()
    return fields or None


def read_gainmap_jpeg(path: Union[str, Path]) -> GainMapFile:
    """Take a gain-map JPEG apart into its two streams and its metadata.

    Parameters
    ----------
    path
        An ISO 21496-1 / Ultra HDR JPEG, as written by
        :func:`write_gain_map_jpeg`.

    Returns
    -------
    GainMapFile
        The two raw JPEG streams, the gain map decoded out of the auxiliary one
        (``data`` in ``[0, 1]``, ``min_log2``/``max_log2``/``gamma`` from the
        XMP), the raw ``hdrgm:`` fields, and the map's absolute offset and
        length.

    Raises
    ------
    MoonlibError
        If the file cannot be read, holds fewer than two JPEG streams, has no
        MPF or XMP metadata, or lacks the GainMapMin / GainMapMax pair the
        decoder needs.
    """
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise MoonlibError(f"cannot read {source}: {exc}") from exc

    streams = _jpeg_streams(data)
    if len(streams) < 2:
        raise MoonlibError(
            f"{source} has {len(streams)} JPEG stream(s); it carries no gain "
            "map."
        )

    main_start, main_end = streams[0]
    if main_start != 0:
        raise MoonlibError(f"{source} does not begin with a JPEG SOI.")

    xmp_fields: Optional[Dict[str, str]] = None
    gain_range: Optional[Tuple[int, int]] = None
    _unused_end, segments = _scan_jpeg(data, main_start)
    for marker, payload_start, payload_end in segments:
        if marker == 0xE1 and xmp_fields is None:
            xmp_fields = _parse_xmp(data[payload_start:payload_end])
        elif marker == 0xE2 and gain_range is None:
            gain_range = _parse_mpf(data, payload_start, payload_end)

    if gain_range is None:
        # An MPF is mandatory in the format; falling back to the second stream
        # keeps a hand-assembled file readable rather than refusing it.
        gain_range = (streams[1][0], streams[1][1] - streams[1][0])
    gain_offset, gain_length = gain_range
    if gain_offset + gain_length > len(data) or gain_offset < main_end:
        raise MoonlibError(
            f"{source}: the MPF points at bytes {gain_offset}.."
            f"{gain_offset + gain_length}, which are not an appended JPEG."
        )
    gain_jpeg = data[gain_offset:gain_offset + gain_length]
    if gain_jpeg[:2] != b"\xff\xd8" or gain_jpeg[-2:] != b"\xff\xd9":
        raise MoonlibError(
            f"{source}: the gain map is not a complete JPEG stream."
        )

    if xmp_fields is None:
        raise MoonlibError(
            f"{source} has no XMP APP1 segment; without the hdrgm: fields the "
            "gain map cannot be interpreted."
        )
    for required in ("GainMapMin", "GainMapMax"):
        if required not in xmp_fields:
            raise MoonlibError(
                f"{source}: the XMP block has no {required}, so the gain map "
                "range is unknown."
            )
    parameters = _gain_parameters(xmp_fields)

    Image = _pillow_image()
    try:
        with Image.open(io.BytesIO(gain_jpeg)) as handle:
            decoded = np.asarray(handle.convert("L"), dtype=np.float32)
    except Exception as exc:
        raise MoonlibError(
            f"{source}: Pillow could not decode the gain map JPEG: {exc}"
        ) from exc
    if decoded.ndim != 2:  # pragma: no cover - Pillow always gives 2-D here
        raise MoonlibError("the decoded gain map is not a single channel.")

    measured = GainMap(
        data=np.clip(decoded / 255.0, 0.0, 1.0).astype(np.float32),
        min_log2=parameters["min_log2"],
        max_log2=parameters["max_log2"],
        gamma=parameters["gamma"],
        offset_sdr=parameters["offset_sdr"],
        offset_hdr=parameters["offset_hdr"],
        capacity_min=parameters["capacity_min"],
        capacity_max=parameters["capacity_max"],
    )
    return GainMapFile(
        main_jpeg=data[main_start:main_end],
        gain_jpeg=gain_jpeg,
        gain_map=measured,
        metadata=dict(xmp_fields),
        gain_offset=gain_offset,
        gain_length=gain_length,
    )


# ---------------------------------------------------------------------------
# from a render to a pair
# ---------------------------------------------------------------------------
def sdr_hdr_pair(render: Any, *, stops: float = 3.0) -> Tuple[np.ndarray, np.ndarray]:
    """The SDR display rendition and the scene-referred light behind it.

    The renderer keeps both halves of the picture and they are not the same
    picture: ``render.image`` has already been through the exposure and the tone
    curve, while ``sunlit`` and ``earthshine`` are the radiance factors that
    went in.  This function pairs them up so that :func:`gain_map` can measure
    what the tone curve did.

    Parameters
    ----------
    render
        A :class:`moonlib_s.models.MoonRender`, or anything with the same four
        attributes.  ``image`` is the ``(h, w, 3)`` display rendition,
        ``sunlit`` and ``earthshine`` are float32 radiance factors (either may
        be ``None`` as long as one is present), and
        ``stats["exposure_scale"]`` is the scalar the renderer exposed with.
    stops
        Size of the HDR range to keep: the returned ``hdr`` is clipped at
        ``2 ** stops``.

    Returns
    -------
    (sdr, hdr)
        ``sdr`` is the clipped float32 display image; ``hdr`` is the same shape,
        linear light.  Its bright end -- the 99.9th percentile of the scene --
        sits at ``2 ** stops``, so the SDR white ends up ``2 ** stops`` times
        lower and the gain map carries that much of a boost.

    Raises
    ------
    MoonlibError
        If the render has no image, neither radiance component, a component of
        the wrong shape, a black display image, no positive radiance at all, or
        a non-positive exposure scale.

    Notes
    -----
    The reference is the 99.9th percentile of the *scene*, not the display
    image's white and not its maximum.  The white is what the SDR exposure was
    metered on, so anchoring there makes every gain zero (see the comment in the
    body: that is exactly the bug which made HDR saves look unchanged); the
    maximum lets one hot pixel -- a specular fleck, a label drawn in pure white
    -- swallow the whole range.  The 99.9th percentile is the same statistic the
    renderer itself meters on, which keeps the two ends of the pair talking
    about the same picture.
    """
    stops_value = _finite_float(stops, "stops")
    if stops_value <= 0.0:
        raise MoonlibError(f"stops must be positive, got {stops_value!r}.")

    image = getattr(render, "image", None)
    if image is None:
        raise MoonlibError("the render has no image to expose.")
    sunlit = getattr(render, "sunlit", None)
    earthshine = getattr(render, "earthshine", None)
    if sunlit is None and earthshine is None:
        raise MoonlibError(
            "the render has no radiance components (sunlit and earthshine are "
            "both None); re-render with keep_components enabled."
        )

    sdr = np.clip(np.asarray(image, dtype=np.float32), 0.0, 1.0)
    if sdr.ndim != 3:  # pragma: no cover - renderer always gives 3 channels
        raise MoonlibError(
            f"the render image must be (height, width, 3), got {sdr.shape}."
        )

    linear = np.zeros_like(sdr)
    for name, component in (("sunlit", sunlit), ("earthshine", earthshine)):
        if component is None:
            continue
        values = _image_array(component, name)
        if values.shape != sdr.shape:
            raise MoonlibError(
                f"render.{name} is {values.shape} but the image is "
                f"{sdr.shape}."
            )
        linear = linear + values.astype(np.float32)

    stats = getattr(render, "stats", None) or {}
    scale = _finite_float(stats.get("exposure_scale", 1.0), "exposure_scale")
    if scale <= 0.0:
        raise MoonlibError(
            f"exposure_scale must be positive, got {scale!r}."
        )
    linear = linear * scale

    # The anchor is the bright end of the *scene*, and the HDR rendition puts it
    # at the top of the range the reader asked for; the SDR white then sits
    # ``2 ** stops`` times lower.
    #
    # This is the only anchor that leaves a gain map with anything in it.  The
    # SDR rendition is a plain linear scale of the scene with a clip on top, so
    # below the clip ``srgb_to_linear(sdr) == linear`` exactly -- anchoring the
    # HDR rendition on the SDR white (which the first version of this function
    # did) therefore produced a gain of *zero* everywhere except the handful of
    # clipped pixels.  Measured on a real 256 px disk render: 22 pixels out of
    # 65 536 carried any gain at all, the largest 0.19 stops, which is why an
    # HDR save looked identical to the plain one on a Mac and on an iPhone.
    #
    # What 动态范围 chooses is how far above the SDR white the picture may go.
    # There is no hidden highlight detail to recover, because this render has
    # none: the moonlit surface spans a few tenths of a stop above the exposure
    # the frame was metered at.
    #
    # The 99.9th percentile rather than the maximum, for the reason given above
    # about the SDR side: one hot pixel must not swallow the whole range.  The
    # rank is taken directly rather than through `np.percentile`, whose default
    # interpolation walks *into* an isolated spike -- with three hot pixels in a
    # 32x32 frame it lands a third of the way up one of them.  "The value 99.9 %
    # of the frame is at or below" is both the honest reading and the stable one.
    positive = np.sort(linear[linear > 0.0])
    reference = 0.0
    if positive.size:
        rank = min(positive.size - 1, int(0.999 * (positive.size - 1)))
        reference = float(positive[rank])
    if not reference > 0.0 or not float(np.max(sdr)) > 0.0:
        raise MoonlibError(
            "the render has no positive radiance (or a black display image), "
            "so there is nothing to build an HDR rendition from."
        )
    ceiling = 2.0 ** stops_value
    hdr = np.clip(linear / (reference / ceiling), 0.0, ceiling)
    return sdr.astype(np.float32), hdr.astype(np.float32)
