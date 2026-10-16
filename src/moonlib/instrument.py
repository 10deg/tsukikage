"""Telescope and camera models, for comparing a simulation with a photograph.

A render is a geometric prediction: it says what the surface would look like
from that place at that instant, with perfect optics and no atmosphere.  A
photograph is that scene convolved with the instrument and then sampled by a
pixel grid.  This module supplies the second half, so the two can be put side by
side.

The model is deliberately the standard one:

* **Diffraction** from the circular aperture, whose point-spread function has
  FWHM ``1.028 * wavelength / aperture``.  It is approximated by a Gaussian of
  the same FWHM, which is what a Gaussian blur can represent.
* **Pixel integration**, a box of one pixel width, approximated by a Gaussian
  with the same variance (``sigma = pixel_scale / sqrt(12)``).
* **Seeing**, also quoted as a FWHM and approximated as a Gaussian.  For a real
  photograph this is almost always the dominant term, and on a large aperture it
  is the *only* term that matters.

Variances add, so the total is the quadrature sum.  Everything is carried in
arcseconds and converted to output pixels at the end, because arcseconds are
what the optics are specified in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .errors import MoonlibError

__all__ = [
    "ARCSEC_PER_RADIAN",
    "DEFAULT_WAVELENGTH_NM",
    "DEFAULT_SHARPEN_RADIUS_FACTOR",
    "LUMA_WEIGHTS",
    "Telescope",
    "Camera",
    "ImagingTrain",
    "arcsec_per_pixel",
    "diffraction_core_fwhm_arcsec",
    "psf_core_ratio",
    "fried_parameter_cm",
    "effective_seeing_arcsec",
    "blur_budget",
    "blur_image",
    "diffraction_ring_kernel",
    "diffraction_rings",
    "instrument_blur",
    "sharpen_radius_arcsec",
    "sharpen_image",
    "instrument_sharpen",
    "desaturate",
    "adjust_gain",
    "finish_image",
    "orient_image",
    "rotate_image",
    "add_sensor_noise",
    "instrument_report",
    "DEFAULT_TELESCOPE",
    "DEFAULT_CAMERA",
    "default_train",
    "process_photograph",
]

ARCSEC_PER_RADIAN = 180.0 / math.pi * 3600.0
DEFAULT_WAVELENGTH_NM = 550.0
#: Default sharpening radius, as a multiple of the effective seeing FWHM.
#: Slightly larger than the seeing, so the mask covers a whole blurred spot
#: rather than only its core.
DEFAULT_SHARPEN_RADIUS_FACTOR = 1.2
#: Rec. 709 luma weights, which define what "grey" means here.
LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)
_FWHM_OVER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))


@dataclass(frozen=True)
class Telescope:
    """Aperture, focal ratio and pixel pitch of an imaging train."""

    aperture_mm: float
    focal_ratio: Optional[float] = None
    focal_length_mm: Optional[float] = None
    barlow: float = 1.0
    """Magnification of any amplifier or reducer ahead of the camera.

    The focal length actually presented to the camera is
    ``focal_length_mm * back_focus * barlow``, so a 2x Barlow doubles the plate
    scale.
    """
    back_focus: float = 1.0
    """Effective focal length as a multiple of the nominal one, from back focus.

    A moving-mirror telescope (any SCT or Maksutov) focuses by changing the
    mirror spacing, and that changes the focal length: adding a diagonal,
    a focuser or a camera with a different flange distance moves an f/10
    system to f/11 or f/9.5.  The number here is the ratio the *whole field*
    is scaled by, so it is a magnification and not a distance -- a 1.1 makes
    every target a tenth larger on the sensor, exactly as a Barlow would, which
    is why the two multiply.
    """
    obstruction: float = 0.0
    """Linear central obstruction as a fraction of the aperture.

    A Schmidt-Cassegrain typically runs 0.30-0.35.  The obstruction moves light
    out of the Airy core into the rings, which *narrows* the core slightly and
    therefore sharpens the image a little while lowering its contrast; both
    effects are handled here rather than assumed away.
    """
    name: str = ""

    def __post_init__(self) -> None:
        if self.aperture_mm <= 0.0:
            raise MoonlibError("aperture_mm must be positive.")
        if self.focal_ratio is None and self.focal_length_mm is None:
            raise MoonlibError(
                "give either focal_ratio or focal_length_mm; a telescope has to "
                "have a focal length."
            )
        if self.focal_ratio is not None and self.focal_ratio <= 0.0:
            raise MoonlibError("focal_ratio must be positive.")
        if self.focal_length_mm is not None and self.focal_length_mm <= 0.0:
            raise MoonlibError("focal_length_mm must be positive.")
        if self.barlow <= 0.0:
            raise MoonlibError("barlow must be positive.")
        if self.back_focus <= 0.0:
            raise MoonlibError("back_focus must be positive.")
        if not 0.0 <= self.obstruction < 0.9:
            raise MoonlibError("obstruction must lie in [0, 0.9).")

    @property
    def native_focal_length_mm(self) -> float:
        """Focal length before any Barlow; the millimetre figure wins if given."""
        if self.focal_length_mm is not None:
            return float(self.focal_length_mm)
        return self.aperture_mm * float(self.focal_ratio)

    @property
    def native_focal_ratio(self) -> float:
        return self.native_focal_length_mm / self.aperture_mm

    @property
    def magnification(self) -> float:
        """How much the train magnifies the nominal focal length."""
        return float(self.barlow) * float(self.back_focus)

    @property
    def focal_length_used_mm(self) -> float:
        """Focal length actually presented to the camera."""
        return self.native_focal_length_mm * self.magnification

    @property
    def effective_focal_ratio(self) -> float:
        return self.native_focal_ratio * self.magnification

    def diffraction_fwhm_arcsec(
        self, wavelength_nm: float = DEFAULT_WAVELENGTH_NM
    ) -> float:
        """FWHM of the diffraction core, narrowed by any central obstruction."""
        return diffraction_core_fwhm_arcsec(
            self.aperture_mm, wavelength_nm, self.obstruction
        )

    def airy_first_minimum_arcsec(
        self, wavelength_nm: float = DEFAULT_WAVELENGTH_NM
    ) -> float:
        return (
            ARCSEC_PER_RADIAN
            * 1.22
            * (wavelength_nm * 1e-9)
            / (self.aperture_mm * 1e-3)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name or f"{self.aperture_mm:.0f} mm f/{self.native_focal_ratio:g}",
            "aperture_mm": self.aperture_mm,
            "focal_length_mm": self.native_focal_length_mm,
            "focal_ratio": self.native_focal_ratio,
            "barlow": self.barlow,
            "back_focus": self.back_focus,
            "focal_length_used_mm": self.focal_length_used_mm,
            "effective_focal_ratio": self.effective_focal_ratio,
            "obstruction": self.obstruction,
            "psf_core_ratio": psf_core_ratio(self.obstruction),
            "diffraction_fwhm_arcsec": self.diffraction_fwhm_arcsec(),
            "airy_first_minimum_arcsec": self.airy_first_minimum_arcsec(),
        }


@dataclass(frozen=True)
class Camera:
    """A sensor: how many pixels, and how big each one is."""

    width_px: int
    height_px: int
    pixel_um: float
    color: bool = True
    """Whether the sensor records colour.

    A monochrome sensor sees the same photons but discards the wavelength
    differences, so a simulated frame from one has no colour to keep: the
    desaturation control stops being a choice and becomes a consequence.
    """
    name: str = ""

    def __post_init__(self) -> None:
        if self.width_px < 1 or self.height_px < 1:
            raise MoonlibError("a camera needs at least one pixel per side.")
        if self.pixel_um <= 0.0:
            raise MoonlibError("pixel_um must be positive.")

    @property
    def sensor_width_mm(self) -> float:
        return self.width_px * self.pixel_um * 1e-3

    @property
    def sensor_height_mm(self) -> float:
        return self.height_px * self.pixel_um * 1e-3

    @property
    def megapixels(self) -> float:
        return self.width_px * self.height_px / 1e6

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name
            or f"{self.width_px}x{self.height_px} @ {self.pixel_um:g} um"
            + ("" if self.color else " mono"),
            "width_px": self.width_px,
            "height_px": self.height_px,
            "pixel_um": self.pixel_um,
            "color": bool(self.color),
            "sensor_width_mm": self.sensor_width_mm,
            "sensor_height_mm": self.sensor_height_mm,
            "megapixels": self.megapixels,
        }


def arcsec_per_pixel(focal_length_mm: float, pixel_um: float) -> float:
    """Plate scale of a focal length and a pixel pitch, in arcseconds per pixel."""
    if focal_length_mm <= 0.0:
        raise MoonlibError("focal_length_mm must be positive.")
    if pixel_um <= 0.0:
        raise MoonlibError("pixel_um must be positive.")
    return ARCSEC_PER_RADIAN * (pixel_um * 1e-3) / focal_length_mm


@dataclass(frozen=True)
class ImagingTrain:
    """A telescope with a camera on the back of it.

    This is where the sensor's field of view comes from: the plate scale is set
    by the focal length and pixel pitch, and the frame the sensor covers is that
    scale times the pixel count.
    """

    telescope: Telescope
    camera: Camera

    @property
    def plate_scale_arcsec(self) -> float:
        """Arcseconds of sky per camera pixel."""
        return arcsec_per_pixel(
            self.telescope.focal_length_used_mm, self.camera.pixel_um
        )

    @property
    def field_arcsec(self) -> Tuple[float, float]:
        """Angular size of the sensor, ``(width, height)`` in arcseconds."""
        scale = self.plate_scale_arcsec
        return (self.camera.width_px * scale, self.camera.height_px * scale)

    @property
    def field_deg(self) -> Tuple[float, float]:
        width, height = self.field_arcsec
        return (width / 3600.0, height / 3600.0)

    @property
    def fov_radius_arcsec(self) -> float:
        """Half the diagonal: the radius that actually contains the sensor."""
        width, height = self.field_arcsec
        return 0.5 * math.hypot(width, height)

    @property
    def sampling_arcsec(self) -> float:
        """Finest detail the pixel grid can represent, by Nyquist (2 px)."""
        return 2.0 * self.plate_scale_arcsec

    def to_dict(self) -> Dict[str, Any]:
        width, height = self.field_arcsec
        return {
            "telescope": self.telescope.to_dict(),
            "camera": self.camera.to_dict(),
            "plate_scale_arcsec": self.plate_scale_arcsec,
            "field_arcsec": [width, height],
            "field_deg": [width / 3600.0, height / 3600.0],
            "fov_radius_arcsec": self.fov_radius_arcsec,
            "sampling_arcsec": self.sampling_arcsec,
        }


def effective_seeing_arcsec(
    seeing_fwhm_arcsec: float, frame_selection_gain: float = 1.0
) -> float:
    """Point-spread width actually present in a stacked frame.

    *seeing_fwhm_arcsec* is the site's long-exposure value: what a single frame,
    or an unselected average of many, is blurred by.  Planetary imaging does not
    use either.  It records thousands of short exposures and keeps the sharpest
    few percent, and because the turbulence is frozen into speckles for a few
    milliseconds, the best frames really are much sharper than the average.

    So the width that belongs in a simulation of a *processed* photograph is the
    site seeing divided by a gain.  Treating the site value as the blur -- which
    is what ``frame_selection_gain = 1.0`` does -- overstates the smearing of any
    image that went through a normal stacking workflow.

    How much gain is available depends on ``D / r0``: with a large aperture in
    mediocre seeing there are many speckles per frame and selection buys little,
    while once the seeing is good enough that ``D / r0`` is only a few, the best
    frames approach the diffraction limit.  :func:`fried_parameter_cm` reports
    ``r0`` so the ratio can be checked; a gain of 1.5 to 2 is a realistic range
    for a 16-inch aperture at ``D / r0`` around 4, and 1.0 is the honest choice
    for a single frame.
    """
    if seeing_fwhm_arcsec < 0.0:
        raise MoonlibError("seeing_fwhm_arcsec cannot be negative.")
    if frame_selection_gain <= 0.0:
        raise MoonlibError("frame_selection_gain must be positive.")
    return seeing_fwhm_arcsec / frame_selection_gain


def fried_parameter_cm(
    seeing_fwhm_arcsec: float, wavelength_nm: float = DEFAULT_WAVELENGTH_NM
) -> float:
    """Fried parameter ``r0`` implied by a seeing FWHM, in centimetres.

    From ``FWHM = 0.98 * lambda / r0``.  Its use here is to judge whether frame
    selection can help: it is the number of speckles across the aperture,
    ``(D / r0) ** 2``, that decides.
    """
    if seeing_fwhm_arcsec <= 0.0:
        raise MoonlibError("seeing_fwhm_arcsec must be positive.")
    radians = seeing_fwhm_arcsec / ARCSEC_PER_RADIAN
    return 0.98 * (wavelength_nm * 1e-9) / radians * 100.0


def blur_budget(
    telescope: Telescope,
    *,
    seeing_fwhm_arcsec: float = 0.0,
    wavelength_nm: float = DEFAULT_WAVELENGTH_NM,
    include_pixel: bool = True,
    frame_selection_gain: float = 1.0,
    arcsec_per_pixel: Optional[float] = None,
) -> Dict[str, float]:
    """Standard deviation of the combined point-spread function, in arcseconds.

    Returns the individual contributions as well as the total, because which
    one dominates is the whole question when comparing with a real photograph:
    on a 16-inch aperture the diffraction term is a third of an arcsecond while
    the seeing term is often two, so the telescope is not what limits the image.

    *seeing_fwhm_arcsec* is the site seeing; *frame_selection_gain* is how much
    of it a stacking workflow removes.  See :func:`effective_seeing_fwhm` -- and
    note that leaving the gain at 1.0 describes a single frame, not a processed
    image.

    """
    if seeing_fwhm_arcsec < 0.0:
        raise MoonlibError("seeing_fwhm_arcsec cannot be negative.")
    sigma_diffraction = telescope.diffraction_fwhm_arcsec(wavelength_nm) / _FWHM_OVER_SIGMA
    sigma_pixel = (
        float(arcsec_per_pixel) / math.sqrt(12.0)
        if (include_pixel and arcsec_per_pixel)
        else 0.0
    )
    sigma_seeing = (
        effective_seeing_arcsec(seeing_fwhm_arcsec, frame_selection_gain)
        / _FWHM_OVER_SIGMA
    )
    total = math.sqrt(
        sigma_diffraction ** 2 + sigma_pixel ** 2 + sigma_seeing ** 2
    )
    return {
        "sigma_diffraction_arcsec": sigma_diffraction,
        "sigma_pixel_arcsec": sigma_pixel,
        "sigma_seeing_arcsec": sigma_seeing,
        "sigma_total_arcsec": total,
        "fwhm_total_arcsec": total * _FWHM_OVER_SIGMA,
        "site_seeing_fwhm_arcsec": float(seeing_fwhm_arcsec),
        "frame_selection_gain": float(frame_selection_gain),
        "effective_seeing_fwhm_arcsec": effective_seeing_arcsec(
            seeing_fwhm_arcsec, frame_selection_gain
        ),
    }


def _convolve1d(array: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    """Edge-clamped convolution along one axis."""
    radius = kernel.size // 2
    padding = [(0, 0)] * array.ndim
    padding[axis] = (radius, radius)
    padded = np.pad(array, padding, mode="edge")
    out = np.zeros(array.shape, dtype=np.float32)
    for index, weight in enumerate(kernel):
        window = [slice(None)] * array.ndim
        window[axis] = slice(index, index + array.shape[axis])
        out += float(weight) * padded[tuple(window)]
    return out


def blur_image(
    image: np.ndarray,
    sigma_px: float,
    *,
    strength: float = 1.0,
    truncate: float = 4.0,
) -> np.ndarray:
    """Separable Gaussian blur, in pixels.

    *strength* multiplies the width, so it is the single knob for "how blurred";
    ``0`` returns the image untouched and ``1`` applies the width as given.

    Written out rather than pulled from a library so that the simulation has no
    dependency beyond numpy, and so the kernel is explicit: it is the instrument
    model, and it should be inspectable.
    """
    if sigma_px < 0.0:
        raise MoonlibError("sigma_px cannot be negative.")
    if strength < 0.0:
        raise MoonlibError("strength cannot be negative.")
    values = np.asarray(image, dtype=np.float32)
    sigma_px = float(sigma_px) * float(strength)
    if sigma_px < 1e-3:
        return values.copy()
    radius = max(1, int(math.ceil(truncate * float(sigma_px))))
    offsets = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (offsets / float(sigma_px)) ** 2)
    kernel /= kernel.sum()
    out = values
    for axis in (0, 1):
        out = _convolve1d(out, kernel, axis)
    return out


def instrument_blur(
    image: np.ndarray,
    telescope: Telescope,
    *,
    arcsec_per_pixel: float,
    seeing_fwhm_arcsec: float = 0.0,
    frame_selection_gain: float = 1.0,
    strength: float = 1.0,
) -> np.ndarray:
    """Blur a rendered image the way *telescope* would record it.

    *strength* scales the whole point-spread width: 1.0 applies the model, 0.5
    applies half of it, 0 leaves the image alone.  Use it to stand in for
    whatever a particular processing pipeline recovers, rather than editing the
    optics to get the picture you want.
    """
    if arcsec_per_pixel <= 0.0:
        raise MoonlibError("arcsec_per_pixel must be positive.")
    sigma = blur_budget(
        telescope,
        seeing_fwhm_arcsec=seeing_fwhm_arcsec,
        frame_selection_gain=frame_selection_gain,
        arcsec_per_pixel=arcsec_per_pixel,
    )["sigma_total_arcsec"]
    return blur_image(image, sigma / arcsec_per_pixel, strength=strength)


def instrument_report(
    telescope: Telescope,
    *,
    arcsec_per_pixel: float,
    seeing_fwhm_arcsec: float = 0.0,
    wavelength_nm: float = DEFAULT_WAVELENGTH_NM,
    frame_selection_gain: float = 1.0,
    sharpen_radius_factor: float = DEFAULT_SHARPEN_RADIUS_FACTOR,
    mask_radius_arcsec: Optional[float] = None,
) -> Dict[str, Any]:
    """Everything needed to explain a comparison, in one dictionary.

    *mask_radius_arcsec* overrides *sharpen_radius_factor* when the caller knows
    the width the mask was actually built with -- a page that states it in
    pixels, say -- so the report describes the frame rather than the rule of
    thumb it did not use.  (It cannot be called ``sharpen_radius_arcsec`` here:
    that is the name of the function that works the width out, and a parameter
    of the same name would shadow it for the whole body.)
    """
    sigma = blur_budget(
        telescope,
        seeing_fwhm_arcsec=seeing_fwhm_arcsec,
        wavelength_nm=wavelength_nm,
        frame_selection_gain=frame_selection_gain,
        arcsec_per_pixel=arcsec_per_pixel,
    )
    variance = sigma["sigma_total_arcsec"] ** 2 or 1.0
    for key in ("diffraction", "pixel", "seeing"):
        sigma["%s_share_of_variance" % key] = (
            sigma["sigma_%s_arcsec" % key] ** 2 / variance
        )
    sigma_px = sigma["sigma_total_arcsec"] / arcsec_per_pixel
    sharpen_arcsec = sharpen_radius_arcsec(
        telescope,
        seeing_fwhm_arcsec=seeing_fwhm_arcsec,
        frame_selection_gain=frame_selection_gain,
        radius_factor=sharpen_radius_factor,
        radius_arcsec=mask_radius_arcsec,
    )
    effective_see = effective_seeing_arcsec(
        seeing_fwhm_arcsec, frame_selection_gain
    )
    return {
        "telescope": telescope.to_dict(),
        "sharpen_sigma_arcsec": sharpen_arcsec,
        "sharpen_sigma_px": sharpen_arcsec / arcsec_per_pixel,
        "sharpen_radius_factor": float(sharpen_radius_factor),
        # What the multiple is a multiple *of*, so the two are never confused.
        "sharpen_basis_fwhm_arcsec": effective_see,
        "wavelength_nm": wavelength_nm,
        "seeing_fwhm_arcsec": seeing_fwhm_arcsec,
        "frame_selection_gain": frame_selection_gain,
        "fried_parameter_cm": fried_parameter_cm(seeing_fwhm_arcsec, wavelength_nm)
        if seeing_fwhm_arcsec > 0.0
        else None,
        "speckles_across_aperture": (
            (telescope.aperture_mm / 10.0 / fried_parameter_cm(seeing_fwhm_arcsec, wavelength_nm)) ** 2
            if seeing_fwhm_arcsec > 0.0
            else None
        ),
        "arcsec_per_pixel": arcsec_per_pixel,
        "blur_sigma_px": sigma_px,
        "blur_fwhm_px": sigma_px * _FWHM_OVER_SIGMA,
        "diffraction_limited": (
            sigma["sigma_seeing_arcsec"] <= sigma["sigma_diffraction_arcsec"]
        ),
        **{key: value for key, value in sigma.items()},
    }


@lru_cache(maxsize=64)
def _psf_profile(obstruction: float, samples: int = 512) -> Tuple[float, ...]:
    """Radial profile of the PSF of an annulus pupil, normalised to the peak."""
    grid = (np.arange(samples) - samples // 2) / (samples // 2)
    xx, yy = np.meshgrid(grid, grid)
    radius = np.hypot(xx, yy)
    pupil = ((radius <= 1.0) & (radius >= obstruction)).astype(np.float64)
    if not pupil.any():  # pragma: no cover - guarded by the constructor
        raise MoonlibError("the obstruction leaves no aperture.")
    psf = np.abs(np.fft.fftshift(np.fft.fft2(np.fft.ifftshift(pupil)))) ** 2
    centre = samples // 2
    profile = psf[centre, centre:]
    return tuple((profile / profile[0]).tolist())


@lru_cache(maxsize=64)
def diffraction_core_fwhm_arcsec(
    aperture_mm: float,
    wavelength_nm: float = DEFAULT_WAVELENGTH_NM,
    obstruction: float = 0.0,
) -> float:
    """FWHM of the diffraction core, ``1.028 lambda / D`` times the CO factor."""
    if aperture_mm <= 0.0:
        raise MoonlibError("aperture_mm must be positive.")
    clear = ARCSEC_PER_RADIAN * 1.028 * (wavelength_nm * 1e-9) / (aperture_mm * 1e-3)
    return clear * psf_core_ratio(obstruction)


@lru_cache(maxsize=64)
def _psf_ring_share(
    obstruction: float, samples: int = 512, pupil_radius: float = 32.0
) -> float:
    """Fraction of an annulus pupil's light that falls outside the Airy disk.

    The Airy disk -- everything inside the first dark ring -- holds 83.8% of a
    clear aperture's light, so 16.2% is left over for the rings, and that is
    the light :func:`diffraction_ring_kernel` has to spend.  An obstruction
    moves more of it out: at the 34% of a Schmidt-Cassegrain the share is a
    third, at 50% it is a half.

    Computed rather than quoted, like :func:`psf_core_ratio`, and for the same
    reason: it is free, and it keeps the number tied to the pupil instead of to
    a remembered constant.  The pupil is zero-padded -- it fills
    ``2 * pupil_radius`` of the ``samples`` grid -- because the first dark ring
    of a pupil that fills the grid is barely one sample wide and cannot be
    found, let alone measured, at that resolution.
    """
    obstruction = float(obstruction)
    grid = (np.arange(samples) - samples / 2.0) / float(pupil_radius)
    xx, yy = np.meshgrid(grid, grid)
    radius = np.hypot(xx, yy)
    pupil = ((radius <= 1.0) & (radius >= obstruction)).astype(np.float64)
    if not pupil.any():  # pragma: no cover - guarded by the constructor
        raise MoonlibError("the obstruction leaves no aperture.")
    psf = np.abs(np.fft.fftshift(np.fft.fft2(np.fft.ifftshift(pupil)))) ** 2
    centre = samples // 2
    # The first dark ring is the first minimum after the core.  Smooth first:
    # the transform's own sampling leaves that minimum a sample wide, and a raw
    # search catches a shoulder of the core instead of the minimum itself.
    profile = np.convolve(psf[centre, centre:], np.ones(5) / 5.0, mode="same")
    first_zero = 1
    for index in range(3, len(profile) - 1):
        if profile[index] <= profile[index - 1] and profile[index] < profile[index + 1]:
            first_zero = index
            break
    iy, ix = np.mgrid[0:samples, 0:samples]
    inside = np.hypot(ix - centre, iy - centre) <= first_zero
    return 1.0 - float(psf[inside].sum() / psf.sum())


def psf_core_ratio(obstruction: float, samples: int = 512) -> float:
    """How much a central obstruction narrows the diffraction core.

    Computed rather than quoted: the PSF is the squared modulus of the Fourier
    transform of the annulus pupil, and the ratio is its half-maximum radius
    against the clear aperture's.  The transform is done in units where the
    clear-aperture core is 1, so no absolute calibration is needed -- only the
    ratio is used, multiplied into the analytic ``1.028 lambda / D``.

    The effect is small: 0.975 for a typical Schmidt-Cassegrain.  It is here
    because it is free to compute and because assuming it away would be a
    silent 2.5% error, not because it rescues resolution.
    """
    if obstruction <= 0.0:
        return 1.0
    profile = _psf_profile(round(float(obstruction), 4), samples)
    clear = _psf_profile(0.0, samples)

    def half_max_radius(values) -> float:
        target = 0.5
        for index in range(1, len(values)):
            if values[index] <= target:
                previous = values[index - 1]
                span = previous - values[index]
                if span <= 0.0:  # pragma: no cover - flat profile
                    return float(index)
                offset = (previous - target) / span
                return float(index - 1) + offset
        return float(len(values))  # pragma: no cover - numerical failure

    return half_max_radius(profile) / half_max_radius(clear)


# ---------------------------------------------------------------------------
# post-processing
# ---------------------------------------------------------------------------


def sharpen_radius_arcsec(
    telescope: Telescope,
    *,
    seeing_fwhm_arcsec: float = 0.0,
    radius_factor: float = DEFAULT_SHARPEN_RADIUS_FACTOR,
    frame_selection_gain: float = 1.0,
    radius_arcsec: Optional[float] = None,
) -> float:
    """Gaussian radius of the unsharp mask, in arcseconds.

    The radius is the one number that decides what sharpening does.  A mask
    narrower than the blur lifts only the core and leaves a hard edge; a mask
    much wider than the blur stops being sharpening at all and just changes the
    large-scale shading.  Usable values run from about 0.8 to 2.

    Parameters
    ----------
    radius_factor
        The radius as a multiple of the seeing -- the knob to reach for first.
        The default of 1.2 sits just above the blur, so the mask covers a whole
        blurred spot rather than its centre.
    radius_arcsec
        Set the radius directly in arcseconds instead, which overrides
        *radius_factor*.  Use this once you know what the image wants rather
        than what the optics suggest.

    Notes
    -----
    The seeing used is the *effective* one, after
    :func:`effective_seeing_arcsec` -- the width actually present in a stacked
    frame, which is what the sharpening has to work against.  With no seeing at
    all it falls back to the whole system width, because diffraction is still
    there.
    """
    if radius_arcsec is not None:
        if radius_arcsec <= 0.0:
            raise MoonlibError("radius_arcsec must be positive.")
        return float(radius_arcsec)
    if radius_factor <= 0.0:
        raise MoonlibError("radius_factor must be positive.")
    effective = effective_seeing_arcsec(seeing_fwhm_arcsec, frame_selection_gain)
    basis = (
        effective
        if effective > 0.0
        else blur_budget(telescope)["fwhm_total_arcsec"]
    )
    return radius_factor * basis


def sharpen_image(
    image: np.ndarray,
    radius_px: float,
    *,
    strength: float = 1.0,
    threshold: float = 0.0,
) -> np.ndarray:
    """``image + amount * (image - blur(image))``.

    This is the standard unsharp mask, and it is what most planetary stacking
    tools reduce to once the wavelets are collapsed to one scale.  It boosts the
    contrast of detail at the mask's scale; it cannot restore detail that the
    atmosphere removed, only re-weight what survived.

    Parameters
    ----------
    sigma_px
        Radius of the mask, in pixels.  Use
        :func:`sharpening_sigma_arcsec` to derive it from the optics.
    amount
        Strength.  ``1.0`` doubles the local contrast at that scale.
    threshold
        Detail smaller than this is left alone, which is the usual way to keep
        sharpening from amplifying noise in flat areas.

    Notes
    -----
    Apply this to **linear** radiance, not to a stretched display image: the
    cheap version of this mistake is to sharpen after the sRGB curve, which
    boosts shadows far more than highlights and produces dark halos.
    """
    if radius_px < 0.0:
        raise MoonlibError("radius_px cannot be negative.")
    if strength < 0.0:
        raise MoonlibError("strength cannot be negative.")
    values = np.asarray(image, dtype=np.float32)
    if radius_px < 1e-3 or strength == 0.0:
        return values.copy()
    detail = values - blur_image(values, radius_px)
    if threshold > 0.0:
        detail = np.where(np.abs(detail) >= float(threshold), detail, 0.0)
    return (values + float(strength) * detail).astype(np.float32)


#: Smallest ring radius a kernel can draw as a ring, in pixels.
#:
#: The ring radius is physical -- the aperture's first Airy minimum -- and on a
#: whole-disk frame it is sub-pixel: a 200 mm aperture's 0.68" minimum is
#: 0.36 px at the 1.9"/px such a frame is usually sampled at.  A ring that fine
#: has nowhere to go on the grid, and drawing it anyway collapses it onto the
#: centre pixel's own neighbours, where it reads as a sharpening artefact
#: rather than as the ghost outside an edge.  Two pixels is the smallest radius
#: that still lands a visible ridge on the grid, so a frame too coarse for its
#: own Airy radius gets the ring there and the optics stay in charge of
#: everything else: the share of the light, the one-sidedness and the
#: obstruction scaling.
_MIN_RING_RADIUS_PX = 2.0


def diffraction_ring_kernel(
    radius_px: float,
    *,
    strength: float = 1.0,
    asymmetry: float = 0.75,
    obstruction: float = 0.0,
) -> np.ndarray:
    """A one-sided ring kernel: the halo an obstructed aperture leaves.

    A real telescope's point-spread function is not a Gaussian: an annular pupil
    puts a bright core inside a ring, and anything that breaks the pupil's
    symmetry -- a spider vane, a slightly tilted element, a tracking error --
    throws that ring's energy to one side.  The visible result is what an
    observer calls a "ghost" hanging off one side of a high-contrast edge.

    The kernel is built from those two facts and nothing else: a ridge at
    *radius_px*, and a cosine weighting that keeps it on one side
    (``asymmetry`` 0 makes it symmetric, 1 leaves it entirely one-sided).
    Written out, it is

        ``(1 - share) * delta + share * ring``

    -- the ring's shape carrying *share* of the light, and the rest left where
    it was.  It sums to one, so it moves light rather than adding any, and the
    light the ring carries is taken from the core, which is what the aperture
    physically does.  *share* is not a free parameter: it is the fraction of
    the aperture's own point-spread function that falls outside the Airy disk,
    :func:`_psf_ring_share`.  A larger obstruction moves more light out of the
    core and so puts more into the ring -- 0.16 of it for a clear aperture,
    0.35 at the 34% of a Schmidt-Cassegrain, 0.52 at 50% -- and the number
    comes from the pupil rather than from a fit.

    Notes
    -----
    There is deliberately **no Gaussian core** in this kernel, for two reasons.
    The core is already in the frame: :func:`instrument_blur` convolves the
    render with the diffraction-limited core (``sigma_diffraction_arcsec`` of
    :func:`blur_budget`), so a second one here blurs the picture twice and
    charges the ring for a halo it did not cause -- and on a real frame that
    halo is what the eye sees, because the ring's own contrast is smaller.
    And core and ring then compete for the same normalisation, which is what
    used to hide the ring altogether: at the web page's usual sampling a 200 mm
    aperture's first minimum is 0.36 px, so the ring was drawn at the old floor
    of 1.2 px *inside* a core of sigma 0.48 px and the kernel's own radial
    profile came out monotone, 0.158, 0.074, 0.020, 0.0008 -- there was no ring
    in it to see.

    *strength* is how much of the pattern is applied: the kernel is blended
    with a delta at the centre, ``(1 - s) * delta + s * kernel``, so 0 is no
    change at all and 1 is the full ring.
    """
    if radius_px <= 0.0:
        raise MoonlibError("radius_px must be positive.")
    if not 0.0 <= strength <= 1.0:
        raise MoonlibError("strength must lie in [0, 1].")
    if not 0.0 <= asymmetry <= 1.0:
        raise MoonlibError("asymmetry must lie in [0, 1].")
    radius = float(radius_px)
    span = max(2, int(math.ceil(2.6 * radius)))
    offsets = np.arange(-span, span + 1, dtype=np.float64)
    xx, yy = np.meshgrid(offsets, offsets)
    rr = np.hypot(xx, yy)
    theta = np.arctan2(yy, xx)
    # The Airy pattern's first bright ring is narrow: it runs from the first
    # dark ring to the second, 1.22 to 2.23 lambda/D, so its width is of the
    # same order as its radius, and a broader Gaussian would smear the ghost
    # back over the core it is supposed to sit outside of.
    ring_width = max(0.5, 0.28 * radius)
    ring = np.exp(-0.5 * ((rr - radius) / ring_width) ** 2)
    # One side only: the weight runs from (1 - asymmetry) at the far side to
    # (1 + asymmetry) on the side the ghost appears.
    ring *= 1.0 + asymmetry * np.cos(theta)
    ring = np.maximum(ring, 0.0)
    ring /= ring.sum()
    share = float(strength) * _psf_ring_share(round(float(obstruction), 4))
    kernel = share * ring
    kernel[span, span] += 1.0 - share
    return kernel.astype(np.float32)


def diffraction_rings(
    image: np.ndarray,
    *,
    radius_px: float,
    strength: float = 0.0,
    asymmetry: float = 0.75,
    obstruction: float = 0.0,
) -> np.ndarray:
    """Move a share of the light onto a one-sided ring at *radius_px*.

    The convolution is with :func:`diffraction_ring_kernel`, which is a delta
    plus the ring, so the picture is changed by the ring's pattern *added* to
    it and paid for out of the core:

        ``out = (1 - share) * image + share * (image convolve ring)``

    which is the same as ``image + share * (image convolve ring - image)``.
    Both halves matter.  It is an addition because the alternative -- folding
    the ring into a normalised core-plus-ring kernel, the way this used to work
    -- leaves the ring competing with a wide blur that is stronger than the
    ring is, so the ghost never reads as a ghost; and it is *paid for* because
    a halo that invents light would be a lie about what an aperture does.

    Separable kernels cannot express a ring, so this one is a true 2-D
    convolution -- but the kernel is tiny (the first Airy ring is a few pixels
    across at any sane sampling) and the loop is over the kernel, not the
    image, so it stays an interactive operation.
    """
    values = np.asarray(image, dtype=np.float32)
    if strength <= 0.0 or radius_px <= 0.0:
        return values.copy()
    kernel = diffraction_ring_kernel(
        radius_px, strength=strength, asymmetry=asymmetry, obstruction=obstruction
    )
    # A convolution flips the kernel: without this the ghost would come out on
    # the opposite side to the one the kernel describes.
    kernel = kernel[::-1, ::-1]
    span = kernel.shape[0] // 2
    padded = np.pad(values, [(span, span), (span, span)] + [(0, 0)] * (values.ndim - 2),
                    mode="edge")
    out = np.zeros_like(values, dtype=np.float32)
    for i in range(kernel.shape[0]):
        for j in range(kernel.shape[1]):
            weight = float(kernel[i, j])
            if weight == 0.0:
                continue
            out += weight * padded[i:i + values.shape[0], j:j + values.shape[1]]
    return out


def instrument_sharpen(
    image: np.ndarray,
    telescope: Telescope,
    *,
    arcsec_per_pixel: float,
    seeing_fwhm_arcsec: float = 0.0,
    radius_factor: float = DEFAULT_SHARPEN_RADIUS_FACTOR,
    amount: float = 1.0,
    threshold: float = 0.0,
    frame_selection_gain: float = 1.0,
    radius_arcsec: Optional[float] = None,
    strength: Optional[float] = None,
) -> np.ndarray:
    """Sharpen the way planetary post-processing would, at the seeing scale.

    *radius_factor* sets the mask radius as a multiple of the effective seeing;
    *radius_arcsec* sets it directly.  See
    :func:`sharpening_sigma_arcsec` for which to prefer.
    """
    if arcsec_per_pixel <= 0.0:
        raise MoonlibError("arcsec_per_pixel must be positive.")
    sigma_arcsec = sharpen_radius_arcsec(
        telescope,
        seeing_fwhm_arcsec=seeing_fwhm_arcsec,
        radius_factor=radius_factor,
        frame_selection_gain=frame_selection_gain,
        radius_arcsec=radius_arcsec,
    )
    if strength is None:
        strength = amount
    return sharpen_image(
        image,
        sigma_arcsec / arcsec_per_pixel,
        strength=strength,
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# frame orientation: what the camera saw, rotated and mirrored
# ---------------------------------------------------------------------------
#: Mirrors an image may be flipped by.  A star diagonal or a Newtonian reflector
#: reverses the field, which is a real property of the view and not a mistake in
#: it, so it is modelled here rather than left to the page.
MIRRORS = ("none", "horizontal", "vertical")


def rotate_image(image: np.ndarray, angle_deg: float, *, fill: float = 0.0) -> np.ndarray:
    """Rotate an image counter-clockwise on screen, about its centre.

    Positive is counter-clockwise **as the picture is looked at** (screen axes,
    y down), which is the convention :attr:`MoonRenderSettings.rotation_deg` and
    the page both use.  Multiples of a right angle take the exact index
    transpose; everything else is bilinear, and the corners that come in from
    outside the frame are *fill*.
    """
    values = np.asarray(image, dtype=np.float32)
    angle = float(angle_deg) % 360.0
    if angle > 180.0:
        angle -= 360.0
    if abs(angle) < 1e-9:
        return values.copy()
    for quarter, turn in ((90.0, 1), (180.0, 2), (270.0, 3)):
        if abs(abs(angle) - quarter) < 1e-9:
            turns = turn if angle > 0.0 else (4 - turn) % 4
            return np.ascontiguousarray(np.rot90(values, k=turns, axes=(0, 1)))
    height, width = values.shape[:2]
    theta = math.radians(angle)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    # Work in bands of output rows.  The inverse map needs four float planes and
    # two index planes per pixel; at the page's ceiling (8192 px, three
    # channels) doing the whole frame at once peaked near ten times the size of
    # the image, which is a gigabyte of nothing for a slider move.  A band is a
    # few megabytes, and the answer is identical either way.
    out = np.empty_like(values)
    # A band holds a quarter of a million output pixels, so the six float planes
    # and two index planes the inverse map needs cost about 15 MB however large
    # the frame is.
    band = max(1, min(int(height), int(250_000 // max(1, width))))
    centre_y, centre_x = 0.5 * (height - 1), 0.5 * (width - 1)
    columns = np.arange(width, dtype=np.float32) - centre_x
    for start in range(0, height, band):
        stop = min(height, start + band)
        rows = (np.arange(start, stop, dtype=np.float32) - centre_y)[:, None]
        # Inverse map: where in the source each output pixel comes from.
        source_x = centre_x + columns[None, :] * cos_t - rows * sin_t
        source_y = centre_y + columns[None, :] * sin_t + rows * cos_t
        outside = (source_x < 0.0) | (source_x > width - 1) | (source_y < 0.0) | (source_y > height - 1)
        source_x = np.clip(source_x, 0.0, width - 1)
        source_y = np.clip(source_y, 0.0, height - 1)
        x0 = np.floor(source_x)
        y0 = np.floor(source_y)
        tx = source_x - x0
        ty = source_y - y0
        xi = x0.astype(np.int32)          # int32: half the bytes of intp
        yi = y0.astype(np.int32)
        x1 = np.minimum(xi + 1, width - 1)
        y1 = np.minimum(yi + 1, height - 1)
        if values.ndim > 2:                  # one weight per channel
            tx = tx[..., None]
            ty = ty[..., None]
            outside = outside[..., None]
        top = values[yi, xi] * (1.0 - tx) + values[yi, x1] * tx
        bottom = values[y1, xi] * (1.0 - tx) + values[y1, x1] * tx
        block = top * (1.0 - ty) + bottom * ty
        out[start:stop] = np.where(outside, fill, block)
    return out


def orient_image(
    image: np.ndarray,
    *,
    rotation_deg: float = 0.0,
    mirror: str = "none",
) -> np.ndarray:
    """Mirror and then rotate a frame, which is how the camera was pointed.

    The mirror comes first because that is the order the optics impose: the
    reversal happens at the diagonal, the rotation at the camera rotator (or in
    the observer's hands), and the two do not commute.
    """
    values = np.asarray(image, dtype=np.float32)
    mode = str(mirror or "none").strip().lower()
    if mode in ("h", "horizontal", "x"):
        values = values[:, ::-1]
    elif mode in ("v", "vertical", "y"):
        values = values[::-1]
    elif mode not in ("none", "", "no"):
        raise MoonlibError(
            f"mirror must be one of {', '.join(MIRRORS)}; got {mirror!r}."
        )
    if abs(float(rotation_deg)) > 1e-9:
        values = rotate_image(values, rotation_deg)
    return np.ascontiguousarray(values, dtype=np.float32)


# ---------------------------------------------------------------------------
# sensor noise
# ---------------------------------------------------------------------------
def add_sensor_noise(
    image: np.ndarray,
    *,
    poisson_photons: Optional[float] = None,
    gaussian_sigma: float = 0.0,
    seed: Optional[int] = None,
) -> np.ndarray:
    """Add the two noises a real exposure has, in linear signal units.

    *poisson_photons*
        Photoelectrons that a signal of ``1.0`` corresponds to -- the full-well
        depth expressed in the same units as the signal.  Shot noise is then
        ``sqrt(counts)`` electrons, which is what makes the grain visible in the
        highlights and invisible in the shadows: exactly the opposite of what a
        uniform noise looks like.  ``None`` or ``0`` leaves the signal alone.
    *gaussian_sigma*
        Read noise, as a fraction of full scale, added on top of the shot noise.
    seed
        Fixes the draw.  A page that re-processes the same frame on every slider
        move wants the grain to stay put, or the picture boils.

    Notes
    -----
    The shot noise uses the Gaussian approximation ``counts + sqrt(counts)·N(0,1)``
    rather than sampling a Poisson deviate per pixel.  The two agree to within a
    percent above a few tens of electrons, and this one runs at numpy speed on a
    frame of any size, which is what a slider needs.
    """
    values = np.asarray(image, dtype=np.float32)
    photons = 0.0 if poisson_photons is None else float(poisson_photons)
    sigma = float(gaussian_sigma or 0.0)
    if photons < 0.0 or sigma < 0.0:
        raise MoonlibError("noise cannot be negative.")
    if photons == 0.0 and sigma == 0.0:
        return values.copy()
    generator = np.random.default_rng(seed)
    shape = values.shape
    out = values
    if photons > 0.0:
        counts = np.clip(values, 0.0, None) * photons
        out = (counts + np.sqrt(counts) * generator.standard_normal(shape, dtype=np.float32)) / photons
    if sigma > 0.0:
        out = out + sigma * generator.standard_normal(shape, dtype=np.float32)
    # Deliberately *not* clipped at zero: clamping would rectify the noise in the
    # dark and lift the mean of a shadow by a few percent.  Radiance cannot be
    # negative, but the tone map is where that is enforced (it takes the maximum
    # with zero before any curve), and clipping here would bias everything the
    # sharpening then sees.
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# finishing choices
# ---------------------------------------------------------------------------
def desaturate(image: np.ndarray, amount: float = 1.0) -> np.ndarray:
    """Pull an image towards grey; ``amount=1`` is fully monochrome.

    Lunar colour is real but very slight -- the colour mosaic behind a render
    carries a few percent of spectral variation, mostly from mare versus
    highland composition -- and most published lunar images are processed to
    near-monochrome, either deliberately or because the stack was aligned on a
    single luminance channel.  Reproducing that is a processing choice, not
    physics, which is why it lives here at the end of the chain rather than in
    the renderer.

    Interpolation is linear in the display values: ``amount`` blends between the
    colour image and its luma, so 0.5 halves the distance from grey.
    """
    if not 0.0 <= amount <= 1.0:
        raise MoonlibError("desaturate amount must lie in [0, 1].")
    values = np.asarray(image, dtype=np.float32)
    if values.shape[-1] == 1:
        return values.copy()
    luma = np.einsum("...c,c->...", values, np.asarray(LUMA_WEIGHTS, dtype=np.float32))
    grey = np.repeat(luma[..., None], values.shape[-1], axis=-1)
    return (values + (grey - values) * float(amount)).astype(np.float32)


def adjust_gain(image: np.ndarray, gain: float) -> np.ndarray:
    """Multiply display values; ``gain`` below 1 darkens.

    Clipped into ``[0, 1]`` because this is the end of the display chain, not a
    radiometric operation.
    """
    if gain < 0.0:
        raise MoonlibError("gain cannot be negative.")
    values = np.asarray(image, dtype=np.float32)
    return np.clip(values * float(gain), 0.0, 1.0).astype(np.float32)


def finish_image(
    image: np.ndarray,
    *,
    saturation: float = 1.0,
    gain: float = 1.0,
    offset: float = 0.0,
    gamma: float = 1.0,
    mono: bool = False,
) -> np.ndarray:
    """The last display step: saturation, then level.

    The four controls are applied in a fixed order, and the order is the
    conventional one so that the numbers mean what they usually mean:

    1. **saturation** -- blend towards luma.  Set ``mono=True`` to force it to
       zero regardless, which is what a monochrome sensor does.
    2. **gain** and **offset** -- a linear stretch of the display values.
    3. **gamma** -- ``out ** (1 / gamma)``.  Above 1 lifts the midtones, below
       1 deepens them.  Applied last so it shapes the tone curve rather than
       being undone by the stretch.

    Saturation comes first because luma commutes with a linear stretch: doing it
    the other way round gives the same hue but a different result once clipping
    bites, and doing it first is the one that keeps dark areas neutral.
    """
    if gamma <= 0.0:
        raise MoonlibError("gamma must be positive.")
    if mono:
        saturation = 0.0
    values = np.asarray(image, dtype=np.float32)
    if saturation < 1.0:
        values = desaturate(values, amount=1.0 - float(saturation))
    if gain != 1.0 or offset != 0.0:
        values = np.clip(values * float(gain) + float(offset), 0.0, 1.0)
    if gamma != 1.0:
        values = np.clip(values, 0.0, 1.0) ** (1.0 / float(gamma))
    return values.astype(np.float32)


# ---------------------------------------------------------------------------
# the whole chain, as one call
# ---------------------------------------------------------------------------
def _default_telescope() -> "Telescope":
    """8-inch f/10 Schmidt-Cassegrain: the common starting point."""
    return Telescope(
        aperture_mm=8.0 * 25.4,
        focal_ratio=10.0,
        obstruction=0.34,
        name='8in f/10 SCT',
    )


def _default_camera() -> "Camera":
    """IMX533C: a 1-inch square colour sensor, 3.76 um pixels."""
    return Camera(width_px=3008, height_px=3008, pixel_um=3.76, color=True,
                  name="IMX533C")


#: Telescope used when a caller does not supply one.
DEFAULT_TELESCOPE = _default_telescope()
#: Camera used when a caller does not supply one.
DEFAULT_CAMERA = _default_camera()


def default_train() -> "ImagingTrain":
    """The default telescope and camera, ready to use."""
    return ImagingTrain(DEFAULT_TELESCOPE, DEFAULT_CAMERA)


def process_photograph(
    sunlit: np.ndarray,
    earthshine: Optional[np.ndarray],
    train: Optional["ImagingTrain"] = None,
    *,
    exposure_scale: float = 1.0,
    arcsec_per_pixel: float,
    seeing_fwhm_arcsec: float = 0.0,
    frame_selection_gain: float = 1.0,
    blur_strength: float = 1.0,
    diffraction: float = 0.0,
    sharpen_radius_factor: float = DEFAULT_SHARPEN_RADIUS_FACTOR,
    sharpen_radius_arcsec: Optional[float] = None,
    sharpen_strength: float = 1.0,
    saturation: float = 1.0,
    gain: float = 1.0,
    offset: float = 0.0,
    gamma: float = 1.0,
    tone: str = "srgb",
    rotation_deg: float = 0.0,
    mirror: str = "none",
    noise_photons: Optional[float] = None,
    noise_sigma: float = 0.0,
    noise_seed: Optional[int] = None,
) -> np.ndarray:
    """Turn linear radiance components into a finished, displayable photograph.

    This is the single entry point for the imaging chain, so that a caller --
    a script, the command line, or the web page -- only has to know one name.
    Every stage is also available on its own; see :func:`instrument_blur`,
    :func:`instrument_sharpen`, :func:`apply_tone_mapping` and
    :func:`finish_image`.

    Parameters
    ----------
    sunlit, earthshine
        Linear ``I/F`` components from the renderer.  *earthshine* may be
        ``None``.
    train
        Telescope and camera; the defaults are an 8-inch f/10 SCT and an
        IMX533C.
    exposure_scale
        The scalar the renderer chose, from ``render.stats["exposure_scale"]``.
    arcsec_per_pixel
        Sampling of the render, from ``render.stats["arcsec_per_pixel"]``.
        Everything optical is specified in arcseconds and converted through
        this, so it has to be the render's own value, not the camera's.
    blur_strength
        Overall multiplier on the blur. ``0`` skips the instrument entirely.
    diffraction
        How much of the telescope's own diffraction pattern to convolve in,
        0-1.  The ring radius is the aperture's first Airy minimum, so it needs
        *arcsec_per_pixel* and the telescope to be meaningful; the ring is
        one-sided, which is what a spider vane or a tilted element does to it.
        A frame too coarse to resolve that radius gets the ring at
        :data:`_MIN_RING_RADIUS_PX` instead -- see the note there.
    rotation_deg, mirror
        How the frame is oriented: a camera rotator and a reversing diagonal,
        applied to the radiance before anything measures it.  *mirror* is one of
        :data:`MIRRORS`.
    noise_photons, noise_sigma, noise_seed
        Sensor noise, added between the optics and the sharpening (see
        :func:`add_sensor_noise`).  It belongs there and not at the end: a real
        photograph is noisy *before* it is sharpened, which is why sharpening a
        real stack amplifies grain and sharpening a clean render does not.
    saturation, gain, offset, gamma
        Display controls, documented at :func:`finish_image`.  A monochrome
        camera forces ``saturation`` to zero.

    Notes
    -----
    The blur and the sharpening are applied to **linear** radiance, before the
    tone curve.  Sharpening a stretched image boosts the shadows far more than
    the highlights and leaves dark halos, which is the usual way this is got
    wrong.
    """
    from .moonview import apply_tone_mapping  # local: moonview is a higher layer

    if train is None:
        train = default_train()
    total = np.asarray(sunlit, dtype=np.float32) * float(exposure_scale)
    if earthshine is not None:
        total = total + np.asarray(earthshine, dtype=np.float32) * float(exposure_scale)

    if abs(float(rotation_deg)) > 1e-9 or str(mirror or "none").lower() not in ("none", ""):
        total = orient_image(total, rotation_deg=rotation_deg, mirror=mirror)

    if blur_strength > 0.0 and seeing_fwhm_arcsec >= 0.0:
        total = instrument_blur(
            total,
            train.telescope,
            arcsec_per_pixel=arcsec_per_pixel,
            seeing_fwhm_arcsec=seeing_fwhm_arcsec,
            frame_selection_gain=frame_selection_gain,
            strength=blur_strength,
        )
    if diffraction > 0.0:
        # After the seeing: the aperture's own pattern is applied to the light
        # that actually reaches the focal plane, and the ring is measured in
        # pixels of this frame.  A ring the frame cannot resolve is drawn at
        # the smallest one it can, rather than being dropped: see
        # _MIN_RING_RADIUS_PX.
        ring_px = (
            train.telescope.airy_first_minimum_arcsec()
            / max(1e-6, float(arcsec_per_pixel))
        )
        total = diffraction_rings(
            total,
            radius_px=max(_MIN_RING_RADIUS_PX, ring_px),
            strength=min(1.0, max(0.0, float(diffraction))),
            obstruction=float(train.telescope.obstruction),
        )
    if noise_photons or noise_sigma:
        total = add_sensor_noise(
            total,
            poisson_photons=noise_photons,
            gaussian_sigma=noise_sigma,
            seed=noise_seed,
        )
    if sharpen_strength > 0.0:
        total = instrument_sharpen(
            total,
            train.telescope,
            arcsec_per_pixel=arcsec_per_pixel,
            seeing_fwhm_arcsec=seeing_fwhm_arcsec,
            frame_selection_gain=frame_selection_gain,
            radius_factor=sharpen_radius_factor,
            radius_arcsec=sharpen_radius_arcsec,
            strength=sharpen_strength,
        )
    display = apply_tone_mapping(
        total, tone=tone, auto_exposure=False, exposure_ev=0.0
    )
    return finish_image(
        display,
        saturation=saturation,
        gain=gain,
        offset=offset,
        gamma=gamma,
        mono=not train.camera.color,
    )
