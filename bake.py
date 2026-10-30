#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy==2.5.3", "scipy==1.18.1", "colour-science==0.4.7"]
# ///
"""Bakes Derby's film looks from film and paper datasheets.

    uv run bake.py                          # every look as a .cube in looks/
    uv run bake.py --only hush --out /tmp   # one look, somewhere else
    uv run bake.py --derby-app PATH         # and Derby's LUT strips and grain table

Each look is a film stock, developed and then scanned, projected or printed
(`LOOKS`). The light is followed the way the film would see it: a
photograph's colours become plausible spectra (Jakob and Hanika's sigmoid
model), which expose the film's three layers through their published spectral
sensitivities; the characteristic curves turn exposure into density; the
dyes, an enlarger and a paper (for a print) or a scanner (for a negative)
make the picture. Nothing is hand-tuned per hue: every colour shift comes from
the datasheets.

The film model is RAWmakase's `scripts/film/film-look.py`
(https://github.com/pch/rawmakase/pull/404, MIT licence, Copyright (c) 2026
RAWmakase contributors), as are the curves in `stocks/` and `data/`, read from
Kodak's, Fujifilm's and Agfa's published datasheets. `data/dng_tone.json` is
the Adobe DNG SDK's default tone table (Adobe DNG SDK licence). See
THIRD_PARTY.md.

What differs from RAWmakase: Derby grades a phone's finished JPEG or HEIC, not
a RAW file, so a look's input is display-referred sRGB. The DNG default tone
curve stands in for the phone's own and is undone to estimate the scene
light. A scanned negative can be scanned harder than the film (`contrast`) and
mixed with a share of its print as light (`print`). The output is pulled into
sRGB toward its own luminance.

Outputs:

* `looks/<NAME>.cube`: a 33-point 3D LUT per look, sRGB in and out, for any
  editor that reads Adobe/Resolve .cube files.
* `looks/grain.json`: each look's film grain for `shader/derby_film.frag`:
  weight against Portra 400 scanned, clump size in display pixels, and the
  share of it that is colour.
* With `--derby-app`, Derby's 1089x33 strips (`assets/luts/`, blue tiles of
  red, green down the rows), its pairs, and the per-look grain table
  (`lib/content/derby_film_grain.dart`).
"""
import argparse
import json
import struct
import sys
import warnings
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

warnings.filterwarnings('ignore', module='colour')
import colour  # noqa: E402
from colour.recovery import find_coefficients_Jakob2019, sd_Jakob2019  # noqa: E402

HERE = Path(__file__).resolve().parent

# Each look: the stock; the stops it is exposed over or under (`push`: a
# negative is then balanced by the lab, a slide is simply that much brighter);
# how hard the lab sets its scanner's contrast (`contrast`, 1 is the film's
# own gamma) and where that contrast hinges (`hinge`, stops below mid grey:
# the tone that comes out exactly as it went in); how much of the film's
# own layer mismatch the lab leaves in (`crossover`, 1 is the stock's own,
# negative is over-corrected: shadows and highlights drift into opposite
# casts while grey stays grey); the share of a print on the
# stock's paper mixed in as light (`print`, 1 is a print alone); the stray
# light that lifts the deepest tones of a scan, print or projection (`flare`,
# a share of white; slides default to PROJECTION_FLARE, prints to
# PRINT_FLARE); and then what the lab does with the picture (`finish()`):
# the print's density (`exposure`, stops), its fade (`fade`, black lifted by
# a share of white), how loudly the film's own colour is spoken (`amplify`,
# the film's hue shifts and casts scaled in Oklab) and the result's chroma
# (`colour`).
#
# The intensity is set against what people reach for. Measured the same way,
# the popular filters sit at a mid-tone contrast of 1.1 to 1.3 times the
# photograph's, keep 1.0 to 1.3 times its colour, and above all move the
# whole picture: a third of a stop up into a fade (Juno, Gingham, Valencia)
# or the shadows down (Clarendon, Lo-Fi). A film scan on its own is quieter
# than any of them, which reads as a better version of the photograph rather
# than an expression, so each look is finished to that level: a floating
# look (FEVER, HUSH, RUSH, AFTER) is printed light with a faded black, a
# sinking one (CLUB, ACID) dense.
#
# Every look is fair to every complexion, measured in stops, which is the
# unit skin is seen in. A lab scanner hinged on mid grey lifts pale skin and
# pushes deep skin down, because mid grey is where a pale face's shadow side
# sits: the Shirley-card bias, restated. These scans hinge a stop and a half
# lower, on the middle of a ten-tone complexion ladder, so pale and deep skin
# move by about the same amount in opposite directions around the middle of
# the range of faces; a floating look lifts deep skin at least as much as
# pale; no look takes deep skin down more than half a stop, and deep skin
# keeps between 85% and 150% of its colour (Clarendon gives it 157%) while
# pale skin keeps under 130%. A slide has no lab, so it is exposed a fraction
# of a stop over for the faces in it. Within those bounds each look was
# searched for the most colour and the strongest tone.
LOOKS = {
    # A vivid slide, projected, exposed half a stop over for the faces.
    'flash': {'stock': 'ektachrome-e100vs', 'push': 0.5, 'flare': 0.004},
    # A warm consumer negative a stop over, scanned hard with a quarter of a
    # print and twice its own layer mismatch left in: warm shadows, cool
    # whites. Hot skin, not orange skin.
    'heat': {'stock': 'gold-200', 'push': 1, 'contrast': 1.15, 'hinge': 1.5, 'print': 0.25,
             'crossover': 1.5, 'colour': 1.15},
    # A portrait negative a stop over, the airy pastel of overexposed Portra,
    # scanned soft with its mismatch left in (warm shadows under cool, faded
    # highlights), printed light and faded, its colour kept quiet. (Fujicolor
    # Pro 160S and 400H take a quarter of deep skin's colour away however
    # they are scanned.)
    'hush': {'stock': 'portra-400', 'push': 1, 'contrast': 1.1, 'hinge': 1.5,
             'crossover': 1.5, 'colour': 1.0, 'exposure': 0.15, 'fade': 0.02},
    # The slide with the deepest reds, a quarter stop over for the faces,
    # faded as an old slide is. (Velvia darkens deep skin a full stop at any
    # exposure, and blows pale skin out before it stops: never made for faces.)
    'rush': {'stock': 'kodachrome-64', 'push': 0.25, 'flare': 0.004,
             'amplify': 1.5, 'colour': 1.2, 'exposure': 0.15, 'fade': 0.02},
    # A cool negative a stop under: teal shadows, scanned hard with a quarter
    # of a print, dense.
    'club': {'stock': 'agfa-ultra-100', 'push': -1, 'contrast': 1.15, 'hinge': 1.5, 'print': 0.25,
             'colour': 1.35},
    # Fuji's greens at box speed, scanned a little hard, dense. (A stop under,
    # the look ACID was first made as, drops deep skin into Superia's toe.)
    'acid': {'stock': 'superia-xtra-400', 'push': 0, 'contrast': 1.1, 'hinge': 1.5,
             'colour': 1.5},
    # A fast negative half printed on Endura, printed dense, with its
    # mismatch over-corrected the way a late lab does: blue-black shadows
    # under warm, flash-lit skin.
    'night': {'stock': 'portra-800', 'contrast': 1.2, 'hinge': 1.5, 'print': 0.5,
              'crossover': -1.5, 'colour': 1.1, 'exposure': -0.05},
}
# The names Derby shows. The stocks are named only to credit their
# datasheets: these are models, not the films, and nobody's trademark.
NAMES = {
    'flash': 'FLASH', 'heat': 'FEVER', 'hush': 'HUSH', 'rush': 'RUSH',
    'club': 'CLUB', 'night': 'AFTER', 'acid': 'ACID',
}
# Derby's combinable looks. INK is Derby's own black-and-white curve, not a
# film model, so it lives in the app and is read from its strip.
CREATIVE = ('flash', 'heat', 'hush', 'rush', 'club', 'night', 'acid', 'ink')
# A pair's share of its first-named look. Derby's slider crossfades `a+b`
# and `b+a`, so it moves the pair between 65/35 and 35/65.
PAIR_LEAN = 0.65
SIZE = 33

SHAPE = colour.SpectralShape(380, 730, 5)
WL = np.arange(SHAPE.start, SHAPE.end + 1, SHAPE.interval, dtype=float)
CMFS = colour.MSDS_CMFS['CIE 1931 2 Degree Standard Observer'].copy().align(SHAPE)
D50 = colour.SDS_ILLUMINANTS['D50'].copy().align(SHAPE)
D55 = colour.SDS_ILLUMINANTS['D55'].copy().align(SHAPE)
PRO = colour.RGB_COLOURSPACES['ProPhoto RGB']
SRGB = colour.RGB_COLOURSPACES['sRGB']
PRO_TO_XYZ = PRO.matrix_RGB_to_XYZ
XYZ_TO_PRO = PRO.matrix_XYZ_to_RGB
SRGB_TO_PRO = colour.matrix_RGB_to_RGB(SRGB, PRO, chromatic_adaptation_transform='Bradford')
PRO_TO_SRGB = colour.matrix_RGB_to_RGB(PRO, SRGB, chromatic_adaptation_transform='Bradford')
SRGB_LUMA = np.array([0.2126, 0.7152, 0.0722])

PAPERS = {'negative': 'endura-premier', 'reversal': 'radiance-iii'}
PRINT_FLARE = 0.01
# A projected slide is seen in a room, whose light and the screen's scatter
# lift its deepest tones, as a share of the screen's white. Without it a slide
# darkens deep skin while lifting pale skin, which is two looks depending on
# who is in the photograph.
PROJECTION_FLARE = 0.0125
ENLARGER_K = 3200
VIEWING_K = 5000
REF_H_ISO = 10 ** -1.44 * 400
MID_GRAY = 0.1842

DATA: Path = HERE
TONE_X = TONE_Y = None
SCENE_GRAY = None


def load_data(path):
    """Points the model at a directory holding stocks/, data/ and the tone table."""
    global DATA, TONE_X, TONE_Y, SCENE_GRAY
    DATA = path
    values = np.array(json.loads((path / 'data' / 'dng_tone.json').read_text())['values'])
    assert len(values) == 1025
    TONE_X, TONE_Y = np.linspace(0, 1, 1025), values
    SCENE_GRAY = float(untone(MID_GRAY))


def stock(name):
    return json.loads((DATA / 'stocks' / f'{name}.json').read_text())


def datasheet(name):
    film = stock(name)
    data = json.loads((DATA / 'data' / f'{name}.json').read_text())
    if data.get('sha256') != film['sha256']:
        raise SystemExit(f'{name}: data/ was not read from the datasheet stocks/ names')
    return {**{k: v for k, v in film.items() if k != 'plots'}, 'curves': data['curves']}


def tone(v):
    return np.interp(np.clip(v, 0, 1), TONE_X, TONE_Y)


def untone(v):
    return np.interp(np.clip(v, 0, 1), TONE_Y, TONE_X)


def hue_preserving(rgb, f):
    """A curve on the darkest and brightest channels, the middle one kept in place."""
    rgb = np.clip(rgb, 0, None)
    lo = rgb.min(axis=-1, keepdims=True)
    hi = rgb.max(axis=-1, keepdims=True)
    a, b = f(lo), f(hi)
    span = hi - lo
    t = np.where(span > 1e-8, (rgb - lo) / np.where(span > 1e-8, span, 1), 0)
    return a + (b - a) * t


def srgb_encode(v):
    v = np.clip(v, 0, 1)
    return np.where(v <= 0.0031308, 12.92 * v, 1.055 * np.power(v, 1 / 2.4) - 0.055)


def srgb_decode(v):
    v = np.clip(v, 0, 1)
    return np.where(v <= 0.04045, v / 12.92, np.power((v + 0.055) / 1.055, 2.4))


# ---------------------------------------------------------------------------
# The film model, adapted from RAWmakase (MIT).
# ---------------------------------------------------------------------------

class Curve:
    """A datasheet curve as a function, extended past its ends: flat or straight."""

    def __init__(self, points, extend='flat'):
        p = np.array(points, dtype=float)
        self.x, self.y = p[:, 0], p[:, 1]
        self.extend = extend

    def __call__(self, x):
        return np.interp(x, self.x, self.y)


def characteristic(points, kind):
    """Density against log exposure. A negative's curve continues straight above its
    plotted end and flat below its toe; a slide's flattens at both ends."""
    c = Curve(points, 'flat')
    if kind == 'negative':
        slope = np.polyfit(c.x[-8:], c.y[-8:], 1)[0]

        def f(x):
            return np.where(x > c.x[-1], c.y[-1] + (x - c.x[-1]) * slope, c(x))
        f.x, f.y = c.x, c.y
        return f
    return c


def sensitivity(points):
    c = np.array(points, dtype=float)
    s = 10 ** np.interp(WL, c[:, 0], c[:, 1])
    return np.where((WL >= c[0, 0]) & (WL <= c[-1, 0]), s, 0.)


def spectral(points):
    c = np.array(points, dtype=float)
    return np.clip(np.interp(WL, c[:, 0], c[:, 1]), 0, None)


CHANNELS = ('red', 'green', 'blue')
DYES = ('cyan', 'magenta', 'yellow')


class Film:
    def __init__(self, data):
        self.data = data
        self.kind = data['kind']
        curves = data['curves']
        self.curves = [characteristic(curves['characteristic'][c], self.kind) for c in CHANNELS]
        self.layers = np.stack([sensitivity(curves['sensitivity'][c]) for c in CHANNELS])
        lit = self.layers * D55.values
        self.sens = lit / lit.sum(axis=1, keepdims=True)
        self.eps, self.base, self.calibration = dye_spectra(curves['dye_density'])

    def exposure(self, reflectance):
        return reflectance @ self.sens.T


def dye_spectra(dd):
    base = spectral(dd['minimum']) if 'minimum' in dd else np.zeros(len(WL))
    if all(d in dd for d in DYES):
        eps = np.stack([spectral(dd[d]) for d in DYES])
        if 'visual_neutral' in dd:
            neutral = spectral(dd['visual_neutral'])
            inside = (WL >= 420) & (WL <= 700)
            if np.abs(eps.sum(axis=0) - neutral)[inside].max() < 0.1 * neutral[inside].max():
                return eps, base, ('neutral', None)
        return eps, base, ('solve', None)
    eps, amounts = decompose(np.array(dd['midscale_neutral'], dtype=float),
                             np.array(dd['minimum'], dtype=float) if 'minimum' in dd else None)
    return eps, base, ('reference', amounts)


DYE_PEAKS = ((600., 700.), (510., 580.), (420., 480.))


def bump(wl, peak, left, right):
    return np.exp(-0.5 * ((wl - peak) / np.where(wl < peak, left, right)) ** 2)


def decompose(midscale, minimum):
    from scipy.optimize import least_squares
    wl = midscale[:, 0]
    target = midscale[:, 1] - (np.interp(wl, minimum[:, 0], minimum[:, 1]) if minimum is not None else 0)

    def model(p, x):
        return sum(p[4 * i] * bump(x, *p[4 * i + 1:4 * i + 4]) for i in range(3))

    p0, lo, hi = [], [], []
    for a, b in DYE_PEAKS:
        p0 += [target.max() / 2, (a + b) / 2, 35., 35.]
        lo += [0., a, 15., 15.]
        hi += [5., b, 90., 90.]
    p = least_squares(lambda p: model(p, wl) - target, p0, bounds=(lo, hi)).x
    eps = np.stack([bump(WL, *p[4 * i + 1:4 * i + 4]) for i in range(3)])
    return eps, np.array([p[4 * i] for i in range(3)])


SPECTRUM_VALUE = 0.2
N_HUE, N_SAT = 72, 25


def jakob_shapes():
    """Smooth reflectance spectra over a grid of chromaticities, cached per user."""
    cache = Path.home() / '.cache' / 'derby-film' / f'spectra-{N_HUE}x{N_SAT}-{SPECTRUM_VALUE}.npz'
    if cache.exists():
        z = np.load(cache)
        return z['spectra'], z['residual']
    spectra = np.zeros((N_HUE, N_SAT, len(WL)))
    residual = np.zeros((N_HUE, N_SAT, 3))
    for i in range(N_HUE):
        coefficients = np.zeros(3)
        for j in range(N_SAT):
            rgb = colour.HSV_to_RGB([i / N_HUE, j / (N_SAT - 1), SPECTRUM_VALUE])
            xyz = PRO_TO_XYZ @ rgb
            if j == 0:
                coefficients = np.zeros(3)
            coefficients, _ = find_coefficients_Jakob2019(xyz, CMFS, D50, coefficients_0=coefficients)
            r = sd_Jakob2019(coefficients, SHAPE).values
            spectra[i, j] = r
            residual[i, j] = xyz - reflectance_xyz(r)
        print(f'\rspectra {i + 1}/{N_HUE}', end='', file=sys.stderr)
    print(file=sys.stderr)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, spectra=spectra, residual=residual)
    return spectra, residual


def reflectance_xyz(r):
    w = D50.values[:, None] * CMFS.values
    return (np.asarray(r) @ w) / w[:, 1].sum()


def colorchecker():
    sds = colour.SDS_COLOURCHECKERS['BabelColor Average']
    return np.stack([sd.copy().align(SHAPE).values for sd in sds.values()])


def blackbody(k):
    return colour.sd_blackbody(k, SHAPE).values


class Model:
    def __init__(self, film, paper=None, push=0, contrast=1.0, flare=None, hinge=0.0,
                 crossover=0.0):
        self.film = film
        self.contrast = contrast
        self.flare = flare
        self.hinge = hinge
        self.crossover = crossover
        spectra, residual = jakob_shapes()
        cc = colorchecker()
        k = np.linalg.lstsq(reflectance_xyz(cc), film.exposure(cc), rcond=None)[0].T
        self.unit = np.maximum(film.exposure(spectra) + residual @ k.T, 0) / SPECTRUM_VALUE
        if film.kind == 'negative':
            self.log_h_ref = film.data.get('log_h_ref', np.log10(REF_H_ISO / film.data['iso']))
        else:
            # A slide has no lab to balance it, so `push` is plain exposure:
            # a slide exposed a fraction of a stop over for the faces in it.
            light = blackbody(film.data.get('viewing_illuminant_k', VIEWING_K))
            self.film_dyes = Dyes(film, light, flare=PROJECTION_FLARE if flare is None else flare)
            self.log_h_ref = self.film_dyes.reference_exposure()
        self.log_h_gray = self.log_h_ref + push * np.log10(2)
        self.d_gray = density(film.curves, np.full(3, self.log_h_gray))
        x = self.log_h_ref + np.linspace(-0.6, 0.6, 13)
        self.gamma = np.array([np.polyfit(x, c(x), 1)[0] for c in film.curves])
        self.paper = paper
        if paper is not None:
            if film.kind == 'negative':
                self.film_dyes = Dyes(film, blackbody(ENLARGER_K), reference=self.log_h_ref)
            self.printer = Printer(self.film_dyes, paper, self.d_gray,
                                   PRINT_FLARE if flare is None else flare)
        elif film.kind == 'negative':
            self.matrix = SRGB_TO_PRO
            self.d_min = np.array([c(np.array(-10.)) for c in film.curves])

    def layer_exposure(self, scene):
        scene = np.clip(scene, 0, None)
        hsv = colour.RGB_to_HSV(scene)
        h = hsv[..., 0] * N_HUE
        s = hsv[..., 1] * (N_SAT - 1)
        h0 = np.floor(h).astype(int)
        s0 = np.clip(np.floor(s).astype(int), 0, N_SAT - 2)
        fh, fs = (h - h0)[..., None], np.clip(s - s0, 0, 1)[..., None]
        h0 %= N_HUE
        h1 = (h0 + 1) % N_HUE
        u = self.unit
        unit = ((1 - fh) * (1 - fs) * u[h0, s0] + fh * (1 - fs) * u[h1, s0]
                + (1 - fh) * fs * u[h0, s0 + 1] + fh * fs * u[h1, s0 + 1])
        h = unit * hsv[..., 2:3]
        return self.log_h_gray + np.log10(np.maximum(h, 1e-9) / SCENE_GRAY)

    def display(self, scene):
        """Display-linear ProPhoto of scene-linear ProPhoto colours."""
        d = density(self.film.curves, self.layer_exposure(scene))
        if self.paper is not None:
            return self.printer.display(d)
        if self.film.kind != 'negative':
            return self.film_dyes.view(d)
        # A lab scanner balanced on grey, its contrast set `contrast` times the
        # film's own gamma: the straight part stays neutral, and blacks and
        # whites move apart around mid grey.
        # A film's three layers have three gammas, and a lab balances them away
        # so that grey stays grey at every density. `crossover` leaves a share
        # of that mismatch in (1 is the stock's own, 2 twice it): the hinge tone
        # stays neutral and the shadows and highlights drift apart into the
        # casts the stock's own curves give, one way below, the other above.
        # It is the look of film printed or scanned without a full correction,
        # and it is each stock's own.
        balanced = self.gamma ** (1 - self.crossover) * np.exp(np.log(self.gamma).mean()) ** self.crossover
        steep = self.contrast / balanced
        # Where the scanner's contrast hinges: mid grey, or `hinge` stops
        # below it, the tone that comes out exactly as it went in.
        hinge_gray = SCENE_GRAY * 2 ** -self.hinge
        d_hinge = density(self.film.curves, np.full(3, self.log_h_gray - self.hinge * np.log10(2)))
        positive = hinge_gray * 10 ** ((d - d_hinge) * steep)
        base = hinge_gray * 10 ** ((self.d_min - d_hinge) * steep)
        if self.crossover:
            # The lab's colour balance is on mid grey whatever the contrast
            # hinges on, so with the mismatch left in, grey stays grey and the
            # shadows and highlights take opposite casts: the stock's own
            # below, their complement above. Brightness at grey is unchanged.
            at_gray = hinge_gray * 10 ** ((self.d_gray - d_hinge) * steep)
            neutral = np.exp(np.log(at_gray).mean()) / at_gray
            positive, base = positive * neutral, base * neutral
        positive = hinge_gray * (positive - base) / (hinge_gray - base)
        out = hue_preserving(positive @ self.matrix.T, tone)
        # A scanner's own flare: stray light in its optics lifts the deepest
        # tones by a share of its white, as a viewing room does a print's.
        scan_flare = self.flare or 0.0
        return (out + scan_flare) / (1 + scan_flare)

    def system_gamma(self):
        """How steeply displayed luminance follows scene luminance at mid gray (log-log)."""
        y = [(self.display(np.full((1, 3), SCENE_GRAY * 2 ** e)) @ PRO_TO_XYZ.T)[0, 1] for e in (-0.5, 0.5)]
        return np.log10(y[1] / y[0]) / np.log10(2)


def density(curves, log_h):
    return np.stack([c(log_h[..., i]) for i, c in enumerate(curves)], axis=-1)


def inverse(curve, d):
    x, y = curve.x, curve.y
    if y[-1] < y[0]:
        x, y = x[::-1], y[::-1]
    return float(np.interp(d, np.maximum.accumulate(y), x))


class Dyes:
    def __init__(self, film, light, flare=0., reference=None):
        self.eps, self.base, (how, amounts) = film.eps, film.base, film.calibration
        self.curves = film.curves
        self.flare = flare
        self.light = light / (light * CMFS.values[:, 1]).sum()
        peaks = WL[self.eps.argmax(axis=1)]
        self.bands = np.exp(-0.5 * ((WL[None] - peaks[:, None]) / 15.) ** 2)
        self.bands /= self.bands.sum(axis=1, keepdims=True)
        self.scale = np.ones(3)
        self.d_base = (np.array([c(np.array(-10.)) for c in self.curves])
                       if how == 'reference' else np.zeros(3))
        if how == 'reference':
            self.neutral = amounts
            at = density(self.curves, np.full(3, reference)) - self.d_base
        else:
            self.neutral = np.ones(3) if how == 'neutral' else self.solve_neutral()
            x = np.linspace(-6, 4, 10001)
            mean = np.mean([c(x) for c in self.curves], axis=0)
            log_h = x[np.argmin(np.abs(mean - 1.0))]
            at = density(self.curves, np.full(3, log_h))
        self.scale = at / self.band_density(self.neutral[None])[0]
        d_min = np.array([c.y.min() for c in self.curves])
        self.white = self.xyz(self.amounts(d_min[None]))[0]

    def solve_neutral(self):
        target = 0.1 * self.xyz(np.zeros(3))
        a = np.ones(3)
        for _ in range(50):
            f = self.xyz(a) - target
            jac = np.stack([(self.xyz(a + e * 1e-5) - self.xyz(a)) / 1e-5 for e in np.eye(3)], axis=-1)
            step = np.linalg.solve(jac, f)
            a = np.maximum(a - step, 0)
            if np.abs(step).max() < 1e-9:
                break
        return a

    def band_density(self, a):
        t = 10 ** -(a @ self.eps)
        return -np.log10(np.maximum(t @ self.bands.T, 1e-12)) * self.scale

    def amounts(self, d):
        d = np.asarray(d, dtype=float) - self.d_base
        a = np.maximum(d, 0)
        for _ in range(30):
            f = self.band_density(a) - d
            jac = np.stack([(self.band_density(a + e * 1e-4) - self.band_density(a)) / 1e-4
                            for e in np.eye(3)], axis=-1)
            step = np.linalg.solve(jac, f[..., None])[..., 0]
            a = np.maximum(a - step, 0)
            if np.abs(step).max() < 1e-6:
                break
        return a

    def transmittance(self, a):
        return 10 ** -(self.base + a @ self.eps)

    def xyz(self, a):
        return (self.transmittance(a) * self.light) @ CMFS.values

    def view(self, d):
        shape = d.shape
        xyz = self.xyz(self.amounts(d.reshape(-1, 3)))
        xyz = (xyz + self.flare * self.white) / (1 + self.flare)
        xyz = colour.adaptation.chromatic_adaptation_VonKries(
            xyz, self.white, colour.xy_to_XYZ(PRO.whitepoint), 'Bradford')
        return (xyz @ XYZ_TO_PRO.T).reshape(shape)

    def mid_gray_densities(self):
        lo, hi = 0., 5.
        for _ in range(60):
            s = (lo + hi) / 2
            y = (self.xyz(s * self.neutral)[1] / self.white[1] + self.flare) / (1 + self.flare)
            lo, hi = (lo, s) if y < MID_GRAY else (s, hi)
        return self.band_density(((lo + hi) / 2 * self.neutral)[None])[0] + self.d_base

    def reference_exposure(self):
        lo, hi = -4., 2.
        for _ in range(50):
            mid = (lo + hi) / 2
            d = np.array([[c(np.array(mid)) for c in self.curves]])
            y = (self.view(d) @ PRO_TO_XYZ.T)[0, 1]
            lo, hi = (mid, hi) if y < MID_GRAY else (lo, mid)
        return (lo + hi) / 2


class Printer:
    def __init__(self, film_dyes, paper, d_gray, flare=PRINT_FLARE):
        self.film_dyes = film_dyes
        self.paper = paper
        self.paper_dyes = Dyes(paper, D50.values, flare=flare)
        self.lamp = blackbody(ENLARGER_K)
        target = self.paper_dyes.mid_gray_densities()
        log_h = np.array([inverse(c, t) for c, t in zip(paper.curves, target)])
        self.filtration = log_h - np.log10(self.exposure(self.film_dyes.amounts(d_gray[None]))[0])

    def exposure(self, a):
        return (self.film_dyes.transmittance(a) * self.lamp) @ self.paper.layers.T

    def display(self, d):
        shape = d.shape
        a = self.film_dyes.amounts(d.reshape(-1, 3))
        log_h = np.log10(np.maximum(self.exposure(a), 1e-30)) + self.filtration
        return self.paper_dyes.view(density(self.paper.curves, log_h)).reshape(shape)


# ---------------------------------------------------------------------------
# Derby's side: the looks, the strips, the pairs.
# ---------------------------------------------------------------------------

def strip_grid():
    """Every strip input, shaped (green, blue, red, rgb): a row per green, a tile
    per blue, red across each tile."""
    v = np.linspace(0, 1, SIZE)
    g, b, r = np.meshgrid(v, v, v, indexing='ij')
    return np.stack([r, g, b], axis=-1)


def into_gamut(lin):
    """Linear sRGB pulled toward its own luminance until no channel is negative."""
    y = np.clip(lin @ SRGB_LUMA, 0, 1)[..., None]
    lo = lin.min(axis=-1, keepdims=True)
    t = np.where(lo < 0, y / np.maximum(y - lo, 1e-12), 1.)
    return np.clip(y + (lin - y) * t, 0, 1)


def write_cube(path, name, table):
    """A .cube 3D LUT: sRGB in and out, red changing fastest."""
    rows = [f'TITLE "{name}"', f'LUT_3D_SIZE {SIZE}', 'DOMAIN_MIN 0 0 0', 'DOMAIN_MAX 1 1 1']
    for b in range(SIZE):
        for g in range(SIZE):
            for r in range(SIZE):
                rows.append(' '.join(f'{v:.6f}' for v in np.clip(table[g, b, r], 0, 1)))
    path.write_text('\n'.join(rows) + '\n')


class Look:
    """A stock as Derby renders it: scanned (or projected), with a share of a
    print on the stock's paper mixed in as light."""

    def __init__(self, spec):
        data = datasheet(spec['stock'])
        film = Film(data)
        share = spec.get('print', 0.0)
        push = spec.get('push', 0)
        paper = Film(datasheet(PAPERS[data['kind']])) if share > 0 else None
        flare = spec.get('flare')
        self.primary = Model(film, paper if share >= 1 else None, push,
                             spec.get('contrast', 1.0), flare, spec.get('hinge', 0.0),
                             spec.get('crossover', 0.0))
        self.printed = Model(film, paper, push, flare=flare) if 0 < share < 1 else None
        self.share = share
        self.gamma = self.primary.gamma

    def display(self, scene):
        out = self.primary.display(scene)
        if self.printed is not None:
            out = (1 - self.share) * out + self.share * self.printed.display(scene)
        return out

    def system_gamma(self):
        y = [(self.display(np.full((1, 3), SCENE_GRAY * 2 ** e)) @ PRO_TO_XYZ.T)[0, 1] for e in (-0.5, 0.5)]
        return np.log10(y[1] / y[0]) / np.log10(2)


def build_model(look):
    return Look(LOOKS[look])


# Oklab (Björn Ottosson, 2020): a perceptual space whose hue stays put as
# lightness and chroma change, used only to amplify a look's own colour.
OKLAB_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                     [0.2119034982, 0.6806995451, 0.1073969566],
                     [0.0883024619, 0.2817188376, 0.6299787005]])
OKLAB_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                     [1.9779984951, -2.4285922050, 0.4505937099],
                     [0.0259040371, 0.7827717662, -0.8086757660]])
OKLAB_M1_INV = np.linalg.inv(OKLAB_M1)
OKLAB_M2_INV = np.linalg.inv(OKLAB_M2)


def oklab(lin):
    return np.cbrt(np.maximum(lin @ OKLAB_M1.T, 0)) @ OKLAB_M2.T


def from_oklab(lab):
    return ((lab @ OKLAB_M2_INV.T) ** 3) @ OKLAB_M1_INV.T


def finish(identity, out, spec):
    """What the lab does after the film: the print's density and its fade,
    and how loudly the film's own colour is spoken. Linear sRGB in and out.

    `exposure` moves the whole picture, in stops. `fade` lifts black by a share
    of white, as a print seen in a bright room, or a scan with its black point
    set high, is lifted. `amplify` scales the look's own colour change, the
    hue shifts and casts the film gives each colour relative to the untouched
    photograph, in Oklab, so a film that deepens reds and quiets blues does
    more of exactly that; `colour` then scales the result's chroma. Lightness
    is left to the lab's contrast and hinge."""
    out = out * 2 ** spec.get('exposure', 0.0) + spec.get('fade', 0.0)
    k, g = spec.get('amplify', 1.0), spec.get('colour', 1.0)
    if k != 1.0 or g != 1.0:
        before = oklab(np.clip(identity, 0, 1))
        after = oklab(np.maximum(out, 0))
        ab = (before[..., 1:] + k * (after[..., 1:] - before[..., 1:])) * g
        out = from_oklab(np.concatenate([after[..., :1], ab], axis=-1))
    return out


def render(spec, srgb):
    """Encoded sRGB colours through a look, as the strip is baked."""
    look = Look(spec)
    lin = srgb_decode(srgb)
    scene = hue_preserving(lin @ SRGB_TO_PRO.T, untone)
    out = look.display(scene.reshape(-1, 3)).reshape(srgb.shape) @ PRO_TO_SRGB.T
    return srgb_encode(into_gamut(finish(lin, out, spec)))


def bake(look, data_dir):
    """The look's strip as sRGB-encoded floats, shaped like `strip_grid()`."""
    load_data(data_dir)
    return look, render(LOOKS[look], strip_grid())


def sample(table, rgb):
    """Trilinear lookup of encoded colours in a strip-shaped table."""
    p = np.clip(rgb, 0, 1) * (SIZE - 1)
    i0 = np.minimum(np.floor(p).astype(int), SIZE - 2)
    f = p - i0
    out = np.zeros_like(rgb)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = ((f[..., 0] if dr else 1 - f[..., 0])
                     * (f[..., 1] if dg else 1 - f[..., 1])
                     * (f[..., 2] if db else 1 - f[..., 2]))
                out += w[..., None] * table[i0[..., 1] + dg, i0[..., 2] + db, i0[..., 0] + dr]
    return out


def mix(a, b, share):
    """Two encoded tables mixed in light, `share` of the first."""
    return srgb_encode(share * srgb_decode(a) + (1 - share) * srgb_decode(b))


def through_filter(table):
    """A look's colour at the photograph's own brightness: what a colour filter
    in front of black-and-white film gives it. INK's tone is INK's own, so a
    look's contrast and INK's are never stacked, which pushed deep skin down
    twice over."""
    lin = srgb_decode(table)
    source = srgb_decode(strip_grid())
    y_look = np.maximum(lin @ SRGB_LUMA, 1e-6)[..., None]
    y_source = (source @ SRGB_LUMA)[..., None]
    return srgb_encode(np.clip(lin * y_source / y_look, 0, 1))


def pair(tables, a, b):
    if b == 'ink':
        return sample(tables['ink'], through_filter(tables[a]))
    if a == 'ink':
        return mix(sample(tables['ink'], through_filter(tables[b])), tables['ink'], 0.5)
    return mix(tables[a], tables[b], PAIR_LEAN)


def write_png(path, table):
    rgb = np.round(np.clip(table, 0, 1) * 255).astype(np.uint8).reshape(SIZE, SIZE * SIZE, 3)
    raw = b''.join(b'\x00' + row.tobytes() for row in rgb)

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))

    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', struct.pack('>IIBBBBB', SIZE * SIZE, SIZE, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def read_png(path):
    """An 8-bit RGB strip, as encoded floats shaped like `strip_grid()`."""
    blob = path.read_bytes()
    pos, idat, header = 8, b'', None
    while pos < len(blob):
        length, kind = struct.unpack('>I4s', blob[pos:pos + 8])
        body = blob[pos + 8:pos + 8 + length]
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', body)
        elif kind == b'IDAT':
            idat += body
        pos += 12 + length
    width, height, depth, colour_type, _, _, interlace = header
    if (depth, colour_type, interlace) != (8, 2, 0):
        raise SystemExit(f'{path}: expected an 8-bit RGB PNG')
    raw = zlib.decompress(idat)
    stride = width * 3
    rows, prev = [], bytearray(stride)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            left = line[x - 3] if x >= 3 else 0
            up = prev[x]
            corner = prev[x - 3] if x >= 3 else 0
            if kind == 1:
                line[x] = (line[x] + left) & 255
            elif kind == 2:
                line[x] = (line[x] + up) & 255
            elif kind == 3:
                line[x] = (line[x] + (left + up) // 2) & 255
            elif kind == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                line[x] = (line[x] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
        rows.append(bytes(line))
        prev = line
    return np.frombuffer(b''.join(rows), np.uint8).reshape(SIZE, SIZE, SIZE, 3) / 255.


# Grain, as RAWmakase reads it from a datasheet: a published rms granularity
# (σD x 1000 through a 48 µm aperture) is density noise, divided by the film's
# gamma it is exposure noise, which the scan, projection or print stretches by
# the system's gamma; Kodak's negatives publish a Print Grain Index instead,
# where four units are one just-noticeable step. Both land in Lightroom-style
# Amount units, where Portra 400's index of 37 is an Amount of 3: the grain
# Derby's slider is authored against, so it is 1.0 here.
GRAIN_NOISE_PER_AMOUNT = 0.0093
GRAIN_APERTURE = 1.15
PGI_REF = (37, 3)
# RAW is graded by nothing, so it borrows the reference grain; INK is a
# black-and-white stock Derby has no datasheet for, so it is stated: a fast
# panchromatic film's coarse, colourless grain.
RAW_GRAIN = {'amount': 1.0, 'size': 1.6, 'colour': 0.3}
INK_GRAIN = {'amount': 1.35, 'size': 1.9, 'colour': 0.0}
# How much of a film's grain is colour rather than brightness: a scanned
# negative's three dye clouds show; a print's paper and a slide's own dyes
# hide most of it.
GRAIN_COLOUR = {'scan': 0.35, 'print': 0.22, 'reversal': 0.18}


def grain_amount(data, model):
    published = data.get('grain', {})
    pgi = {k: v for k, v in published.get('print_grain_index', {}).items() if '135' in k and '4x6' in k}
    if 'rms' in published:
        noise = (np.log(10) * published['rms'] / 1000 * GRAIN_APERTURE
                 * model.system_gamma() / np.abs(model.gamma).mean())
        return noise / GRAIN_NOISE_PER_AMOUNT
    if pgi:
        value, = pgi.values()
        index = 21 if str(value).startswith('<') else float(value)
        return PGI_REF[1] * 1.2 ** ((index - PGI_REF[0]) / 4)
    return 2 + 3 * np.log2(data['iso'] / 100)


def grain_character(look):
    """A look's grain: its weight against the reference, the size of a clump in
    device pixels (faster film, bigger clumps), and its colour share."""
    spec = LOOKS[look]
    data = datasheet(spec['stock'])
    model = build_model(look)
    amount = float(np.clip(grain_amount(data, model) / PGI_REF[1], 0.5, 1.8))
    size = float(np.clip(0.9 + 0.35 * np.log2(data['iso'] / 100), 0.9, 2.2))
    kind = ('reversal' if data['kind'] == 'reversal'
            else 'print' if spec.get('print', 0) >= 0.5 else 'scan')
    return {'amount': amount, 'size': size, 'colour': GRAIN_COLOUR[kind]}


def write_grain_table(path, characters):
    rows = ''.join(
        f"      '{key}': DerbyFilmGrain(amount: {c['amount']:.3f}, size: {c['size']:.3f}, "
        f"colour: {c['colour']:.3f}),\n"
        for key, c in characters.items())
    path.write_text(
        '// GENERATED by derby-film (bake.py --derby-app) from the film datasheets.\n'
        '// Do not edit; run the tool again.\n\n'
        "import 'derby_film_grain_model.dart';\n\n"
        '/// Each look\'s grain, keyed by its strip name.\n'
        'const Map<String, DerbyFilmGrain> derbyFilmGrainByLook =\n'
        '    <String, DerbyFilmGrain>{\n'
        f'{rows}    }};\n')


# Grain, as RAWmakase reads it from a datasheet: a published rms granularity
# (σD x 1000 through a 48 µm aperture) is density noise, divided by the film's
# gamma it is exposure noise, which the scan, projection or print stretches by
# the system's gamma; Kodak's negatives publish a Print Grain Index instead,
# where four units are one just-noticeable step. Both land in Lightroom-style
# Amount units, where Portra 400's index of 37 is an Amount of 3: the grain
# Derby's slider is authored against, so it is 1.0 here.
GRAIN_NOISE_PER_AMOUNT = 0.0093
GRAIN_APERTURE = 1.15
PGI_REF = (37, 3)
# RAW is graded by nothing, so it borrows the reference grain; INK is a
# black-and-white stock Derby has no datasheet for, so it is stated: a fast
# panchromatic film's coarse, colourless grain.
RAW_GRAIN = {'amount': 1.0, 'size': 1.6, 'colour': 0.3}
INK_GRAIN = {'amount': 1.35, 'size': 1.9, 'colour': 0.0}
# How much of a film's grain is colour rather than brightness: a scanned
# negative's three dye clouds show; a print's paper and a slide's own dyes
# hide most of it.
GRAIN_COLOUR = {'scan': 0.35, 'print': 0.22, 'reversal': 0.18}


def grain_amount(data, model):
    published = data.get('grain', {})
    pgi = {k: v for k, v in published.get('print_grain_index', {}).items() if '135' in k and '4x6' in k}
    if 'rms' in published:
        noise = (np.log(10) * published['rms'] / 1000 * GRAIN_APERTURE
                 * model.system_gamma() / np.abs(model.gamma).mean())
        return noise / GRAIN_NOISE_PER_AMOUNT
    if pgi:
        value, = pgi.values()
        index = 21 if str(value).startswith('<') else float(value)
        return PGI_REF[1] * 1.2 ** ((index - PGI_REF[0]) / 4)
    return 2 + 3 * np.log2(data['iso'] / 100)


def grain_character(look):
    """A look's grain: its weight against the reference, the size of a clump in
    device pixels (faster film, bigger clumps), and its colour share."""
    spec = LOOKS[look]
    data = datasheet(spec['stock'])
    model = build_model(look)
    amount = float(np.clip(grain_amount(data, model) / PGI_REF[1], 0.5, 1.8))
    size = float(np.clip(0.9 + 0.35 * np.log2(data['iso'] / 100), 0.9, 2.2))
    kind = ('reversal' if data['kind'] == 'reversal'
            else 'print' if spec.get('print', 0) >= 0.5 else 'scan')
    return {'amount': amount, 'size': size, 'colour': GRAIN_COLOUR[kind]}


def write_grain_table(path, characters):
    rows = ''.join(
        f"      '{key}': DerbyFilmGrain(amount: {c['amount']:.3f}, size: {c['size']:.3f}, "
        f"colour: {c['colour']:.3f}),\n"
        for key, c in characters.items())
    path.write_text(
        '// GENERATED by derby-film (bake.py --derby-app) from the film datasheets.\n'
        '// Do not edit; run the tool again.\n\n'
        "import 'derby_film_grain_model.dart';\n\n"
        '/// Each look\'s grain, keyed by its strip name.\n'
        'const Map<String, DerbyFilmGrain> derbyFilmGrainByLook =\n'
        '    <String, DerbyFilmGrain>{\n'
        f'{rows}    }};\n')


def report(look, table):
    """Grays and two complexions, in and out, in units of 255."""
    def at(v):
        return np.round(sample(table, np.array([v], dtype=float) / 255.)[0] * 255).astype(int)
    grays = ' '.join(f'{v}->{"/".join(map(str, at([v, v, v])))}' for v in (40, 118, 200))
    skin = ' '.join(f'{"/".join(map(str, c))}->{"/".join(map(str, at(c)))}'
                    for c in ((196, 150, 128), (112, 76, 60)))
    return f'{look:6} grays {grays}  skin {skin}'


def write_image(path, rgb):
    """An 8-bit RGB PNG from floats shaped (height, width, 3)."""
    data = np.round(np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    height, width, _ = data.shape
    raw = b''.join(b'\x00' + row.tobytes() for row in data)

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))

    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def swatches(path, tables):
    """A chart through every look: the ColorChecker's 24 patches as an sRGB
    camera would record them, a grey ramp and a hue sweep. One row per look,
    the untouched chart first."""
    cc = reflectance_xyz(colorchecker())
    patches = srgb_encode(into_gamut(cc @ colour.RGB_COLOURSPACES['sRGB'].matrix_XYZ_to_RGB.T))
    cell = 28
    ramp = np.linspace(0, 1, 12 * cell)
    hue = colour.HSV_to_RGB(np.stack([np.linspace(0, 1, 12 * cell, endpoint=False),
                                      np.full(12 * cell, 0.7), np.full(12 * cell, 0.85)], -1))

    def chart():
        rows = [np.repeat(np.repeat(patches[i * 6:(i + 1) * 6][None], cell, 0), 2 * cell, 1)
                for i in range(4)]
        grey = np.repeat(np.stack([ramp] * 3, -1)[None], cell // 2, 0)
        sweep = np.repeat(hue[None], cell // 2, 0)
        return np.concatenate(rows + [grey, sweep], 0)

    base = chart()
    blocks = [base]
    for look, table in tables.items():
        blocks.append(sample(table, base))
    gap = np.ones((6, base.shape[1], 3))
    write_image(path, np.concatenate([x for b in blocks for x in (b, gap)][:-1], 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, default=HERE / 'looks', help='where the .cube files go')
    ap.add_argument('--only', choices=sorted(LOOKS), help='bake one look')
    ap.add_argument('--derby-app', type=Path,
                    help="Derby's app directory (apps/mobile-flutter/derby): also write its strips, "
                         'pairs and grain table')
    ap.add_argument('--swatches', type=Path, help='also write a chart through every look to this PNG')
    ap.add_argument('-j', '--jobs', type=int, default=4)
    args = ap.parse_args()
    load_data(HERE)
    jakob_shapes()
    looks = [args.only] if args.only else list(LOOKS)
    args.out.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(args.jobs) as pool:
        tables = dict(pool.map(bake, looks, [HERE] * len(looks)))
    for look in looks:
        write_cube(args.out / f'{NAMES[look]}.cube', NAMES[look], tables[look])
        print(report(look, tables[look]))
    if args.swatches:
        swatches(args.swatches, {look: tables[look] for look in looks})
    characters = {'raw': RAW_GRAIN}
    for look in LOOKS:
        characters[look] = grain_character(look)
        c = characters[look]
        print(f'{look:6} grain {c["amount"]:.2f} x, {c["size"]:.2f} px, colour {c["colour"]:.2f}')
    characters['ink'] = INK_GRAIN
    (args.out / 'grain.json').write_text(json.dumps(
        {NAMES.get(key, key.upper()): {k: round(v, 3) for k, v in c.items()}
         for key, c in characters.items()}, indent=1) + '\n')
    if not args.derby_app:
        return
    luts = args.derby_app / 'assets' / 'luts'
    for look in looks:
        write_png(luts / f'{look}.png', tables[look])
    if args.only:
        return
    tables['ink'] = read_png(luts / 'ink.png')
    count = 0
    for a in CREATIVE:
        for b in CREATIVE:
            if a != b:
                write_png(luts / f'{a}+{b}.png', pair(tables, a, b))
                count += 1
    write_grain_table(args.derby_app / 'lib' / 'content' / 'derby_film_grain.dart', characters)
    print(f'{len(looks)} film looks and {count} pair strips in {luts}')


if __name__ == '__main__':
    main()
