# derby-film

The film looks and film effects from [Derby](https://derbysocial.app), a photo
app for friends. Each look is a film stock modelled from its manufacturer's
published datasheet, rather than a hand-tuned filter. You can use them in your
own editor as `.cube` LUTs, or in a Flutter app as a fragment shader.

![The looks over a test chart](docs/swatches.png)

*From the top: the untouched chart (ColorChecker patches, a grey ramp, a hue
sweep), then NOON, GOLDIE, MOON, CHERRY, POOL, CURFEW and LIME.*

## The looks

| Look | Modelled from | How it's rendered |
|---|---|---|
| NOON | Kodak Ektachrome E100VS | A vivid slide, projected, exposed half a stop over for the faces |
| GOLDIE | Kodak Gold 200 | Exposed a stop over, scanned hard, a quarter of a print mixed in |
| MOON | Kodak Portra 400 | Exposed a stop over, scanned a little hard, a quarter of a print mixed in |
| CHERRY | Kodachrome 64 | The slide with the deepest reds, projected, a quarter stop over for the faces |
| POOL | Agfa Ultra 100 | Exposed a stop under, scanned hard, a quarter of a print mixed in |
| CURFEW | Kodak Portra 800 | Scanned a little hard, a quarter of a print on Kodak Endura Premier mixed in |
| LIME | Fujicolor Superia X-TRA 400 | At box speed, scanned a little hard |

These are models built from published curves, not the films themselves. They
haven't been checked against real scans. The film names credit the datasheets
the curves come from; Derby has no affiliation with Kodak, Kodak Alaris,
Fujifilm or Agfa.

## How a look is made

`bake.py` follows the light through the film:

1. **Undo the phone.** The input is a finished JPEG or HEIC, not a RAW file.
   The tool undoes a standard camera tone curve (the DNG SDK default) to
   estimate the light in the scene.
2. **Expose the film.** Each colour becomes a plausible light spectrum, using
   Jakob and Hanika's 2019 model. The spectrum exposes the film's three layers
   through their published spectral sensitivities.
3. **Develop it.** The characteristic curves turn each layer's exposure into
   dye density. Exposure decides where a colour sits on the film's toe and
   shoulder, which is where film's soft highlights and dense shadows come from.
4. **Make a picture.** One of three ways:
   - **Scanned negative:** each channel is inverted and balanced, as a lab
     scanner does. `contrast` sets the scanner harder than the film, and
     `hinge` is the tone it is balanced on, in stops below mid grey.
   - **Projected slide:** the slide's dyes are seen under projector light.
   - **Print:** an enlarger lamp shines through the film's dyes onto the
     paper's own layers, and the print is seen under daylight. `print` mixes
     a share of the print into a scan, as light.

No colour is adjusted by hand for any hue, and nothing is saturated by hand.
Every colour shift, including how reds deepen and how greens drift, comes
from the datasheets. Every look keeps grey neutral.

## Fair to every complexion

For decades, colour film and its printing were calibrated on pale skin, and
deep skin paid for it. These looks don't repeat that. Each one has to pass a
test across ten complexions, from the palest to the deepest, under warm,
white and cool light:

- **Brightness, in stops:** around the middle of the ladder, a look does to
  deep skin what it does to pale skin: the same amount in opposite directions
  for a look that adds contrast, a lift for both for a look that floats. No
  look takes deep skin down more than half a stop. (Stops, not 8-bit units:
  a unit is a tenth of a stop on pale skin and a third of one on deep skin,
  so a unit bound is secretly three times stricter on deep skin.)
- **Colour:** deep skin keeps between 85% and 150% of its colour, so it is
  never ashy and never the only thing the colour lands on, and pale skin is
  never pushed orange.

The bias is in the lab more than in the film. A lab scanner hinges its
contrast on mid grey, and mid grey is where a pale face's shadow side sits:
that is the Shirley card, the pale reference print labs were balanced on for
decades, restated as arithmetic. Hinged there, any contrast at all lifts
pale skin and pushes deep skin down, and the first version of these looks
passed the test only by having almost no contrast. These scans hinge a stop
and a half lower, on the middle of the complexion ladder, so pale and deep
skin move by about the same amount, in stops, around the middle of the range
of faces. A slide has no lab, so it is exposed a fraction of a stop over for
the faces in it, as a careful photographer would. Five settings in
`bake.py` make a look:

- **`push`:** how far over or under the stock is exposed. A negative is then
  balanced by the lab; a slide is simply that much brighter.
- **`contrast`** and **`hinge`:** how hard the lab sets its scanner, and the
  tone it balances it on.
- **`crossover`:** how much of the film's own layer mismatch the lab leaves
  in. A film's three layers have three gammas; a lab balances them away so
  grey stays grey at every density. Left in, grey stays grey at mid grey and
  the shadows and highlights drift into opposite casts, the stock's own
  (warm shadows under cool highlights on the Kodak negatives); over-corrected
  (a negative value), the other way. It is where a look's parts move in
  different directions, and it is each stock's own.
- **`print`:** how much of a print is mixed in.
- **`flare`:** stray light in the scanner, the projector's room or the
  print's, lifting the deepest tones.

After the film, `finish()` is what the lab does with the picture, and it is
where the looks get their intensity: `exposure` (the print's density, in
stops), `fade` (black lifted by a share of white, as a print seen in a bright
room), `amplify` (the film's own hue shifts and casts, scaled in Oklab, so a
film that deepens reds and quiets blues does more of exactly that) and
`colour` (the result's chroma). The level is set against what people reach
for: measured the same way, the popular Instagram filters sit at a mid-tone
contrast of 1.1 to 1.3 times the photograph's, keep 1.0 to 1.3 times its
colour, and above all move the whole picture, a third of a stop up into a
fade or the shadows down. A film scan on its own is quieter than any of
them, and reads as a better version of the photograph rather than an
expression.

The settings were found by search, as the most colour and the strongest tone
that still pass the test. Three films failed the test
however they were exposed or scanned: Fujicolor Pro 160S and Pro 400H take
about a quarter of deep skin's colour away, and Fujichrome Velvia darkens
deep skin a full stop at any exposure and blows pale skin out before it
stops. MOON, POOL and CHERRY use other films.

A look paired with INK gives INK the look's colour at the photograph's own
brightness, as a colour filter in front of black-and-white film would, and
INK's own tone: the two contrasts are never stacked.

The film model and the datasheet curves come from
[RAWmakase](https://github.com/pch/rawmakase/pull/404) (MIT). RAWmakase reads
the curves straight from the vector paths in each datasheet PDF. Thank you to
its contributors.

## Use the LUTs

`looks/*.cube` are 33-point 3D LUTs, sRGB in and sRGB out. Lightroom (as a
profile), Photoshop, DaVinci Resolve, Premiere, Final Cut and most photo and
video editors can open them. Apply one to a finished, display-referred image
such as a phone photo or an sRGB export, not to log or RAW footage.

## Use the effects (Flutter)

`shader/derby_film.frag` is the shader Derby renders photos with. It applies
the look and five film effects:

| Effect | What it models |
|---|---|
| Diffusion | A mist filter in front of the lens: bright things bloom softly in their own colour |
| Vignette | The light a lens loses toward the corners (cos⁴), added before the look so the film's own shadows darken |
| Glow | Halation: light that bounced off the back of the film and re-exposed the red layer around bright light sources |
| Leak | Light that got into the camera and fogged the film from one edge, in film's reds, ambers and magentas |
| Grain | Film grain whose weight, clump size and colour come from the look's film (`looks/grain.json`) |

Diffusion, vignette, glow and leak are added as light before the look, so the
film renders them the way it renders the rest of the picture. Grain goes on
the finished image. Effect levels run from 0 (off) to 10, and 5 is each
effect's intended strength.

Add the shader to `pubspec.yaml` under `flutter: shaders:`, then:

```dart
final program = await ui.FragmentProgram.fromAsset('shaders/derby_film.frag');
final shader = program.fragmentShader()
  ..setFloat(0, width)            // u_size
  ..setFloat(1, height)
  ..setFloat(2, 1.0)              // u_strength: 0 = no look, 1 = full look
  ..setFloat(3, 33)               // u_cube_size
  ..setFloat(4, grainLevel)       // u_effect_grain, 0–10
  ..setFloat(5, glowLevel)        // u_effect_glow
  ..setFloat(6, leakLevel)        // u_effect_leak (also picks where the leak enters)
  ..setFloat(7, leakSeed % 104729)// u_effect_leak_seed: a new number makes a new leak
  ..setFloat(8, vignetteLevel)    // u_effect_vignette
  ..setFloat(9, diffusionLevel)   // u_effect_diffusion
  ..setFloat(10, 1.0)             // u_effect_scale: render width / display width
  ..setFloat(11, grain.amount)    // from looks/grain.json
  ..setFloat(12, grain.size)
  ..setFloat(13, grain.colour)
  ..setImageSampler(0, photo)
  ..setImageSampler(1, lutStrip);
```

`lutStrip` is the look as a 1089×33 image: 33 tiles of 33×33, where blue
selects the tile, red runs across it and green runs down it. To write the
strips, run `bake.py --derby-app DIR`. It writes them to `DIR/assets/luts/`,
alongside the pairs and grain table Derby itself uses.

If you draw the shader with `ImageFilter.shader` over a widget, Flutter
supplies `u_size` and the first sampler for you.

## Run the baker

```bash
uv run bake.py --swatches docs/swatches.png
```

[uv](https://docs.astral.sh/uv/) installs the pinned dependencies (numpy,
scipy, colour-science). The first run spends about a minute fitting spectra,
then caches them in `~/.cache/derby-film`. To add a look, add an entry to
`LOOKS` in `bake.py`. Any stock in `stocks/` will work, including ones Derby
doesn't use, such as Portra 160 and 400, Ektar, Kodachrome, Provia, Astia and
the Agfa films.

## Limits

- **Modelled, not matched:** nobody has shot or scanned the films to check
  these looks.
- **Assumed where the datasheets say nothing:** a narrow-band densitometer at
  each dye's peak; the three dyes of Kodak's negatives, estimated from a
  neutral's total density; an sRGB scanner; a 3200 K enlarger lamp; 1% print
  viewing flare; and a phone's tone curve that resembles the DNG default.
- **Skin:** every look passes the test above, which uses one ladder of
  skin tones. Still check faces in your own photos, and tell us if a look
  treats someone unfairly.

## Licence

MIT; see [LICENSE](LICENSE). The film model and the datasheet curves are
RAWmakase's, also MIT. `data/dng_tone.json` is Adobe's DNG SDK default tone
table, under the Adobe DNG SDK licence. All three are in
[THIRD_PARTY.md](THIRD_PARTY.md).
