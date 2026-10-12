// derby_film.frag: Derby's film look and film effects, as one Flutter
// fragment shader (runtime_effect GLSL, compiled by impellerc). Ported from
// the Derby app; MIT licence, see LICENSE.
//
// The look is a 3D LUT strip as bake.py --derby-app writes it: 33 tiles of
// 33x33 side by side (blue picks the tile, red runs across it, green down
// it), sampled with tetrahedral interpolation. Then, as in the app:
//
//   1. The light that reaches the film, in linear light, before the look:
//      DIFFUSION (a mist filter's bloom), VIGNETTE (a lens's cos^4 falloff),
//      GLOW (halation around light sources) and LEAK (fog from a frame edge).
//   2. The look.
//   3. GRAIN, whose character (weight, clump size, colour) comes from the
//      look's film: see grain.json, written by bake.py.
//
// Effect levels run 0 (off) to 10; 5 is each effect's authored strength.
// u_effect_scale divides the fragment coordinate for the grain, so a render
// at a larger size than it will be shown (a 1440px master shown at 1236
// device pixels: 1440 / 1236) keeps the grain at one display pixel's pitch.
// Distances for glow, diffusion and leak are shares of the frame, so they
// need no scale.
#include <flutter/runtime_effect.glsl>

uniform vec2 u_size;
uniform float u_strength;
// Deliberately not called u_lut_size: Flutter's SkSL translation reserves
// u_<sampler>_size for every sampler.
uniform float u_cube_size;
uniform float u_effect_grain;
uniform float u_effect_glow;
uniform float u_effect_leak;
uniform float u_effect_leak_seed;
uniform float u_effect_vignette;
uniform float u_effect_diffusion;
uniform float u_effect_scale;
uniform float u_grain_amount;
uniform float u_grain_size;
uniform float u_grain_colour;
uniform sampler2D u_texture_input;
uniform sampler2D u_lut;

out vec4 frag_color;

vec3 lut_point(vec3 index) {
  float width = u_cube_size * u_cube_size;
  float x = ((index.b * u_cube_size) + index.r + 0.5) / width;
  float y = (index.g + 0.5) / u_cube_size;
  return texture(u_lut, vec2(x, y)).rgb;
}

// Tetrahedral interpolation follows one of the six tetrahedra inside a LUT
// cube. It avoids the muddy diagonal transitions trilinear sampling can make
// in saturated split-tone looks while using only four exact LUT points.
vec3 sample_lut(vec3 color) {
  float edge = u_cube_size - 1.0;
  vec3 position = clamp(color, 0.0, 1.0) * edge;
  vec3 low = floor(position);
  vec3 high = min(low + 1.0, vec3(edge));
  vec3 fraction = position - low;
  vec3 c000 = lut_point(low);
  vec3 c111 = lut_point(high);
  vec3 point1;
  vec3 point2;
  float first;
  float second;
  float third;

  if (fraction.r >= fraction.g) {
    if (fraction.g >= fraction.b) {
      point1 = vec3(high.r, low.g, low.b);
      point2 = vec3(high.r, high.g, low.b);
      first = fraction.r;
      second = fraction.g;
      third = fraction.b;
    } else if (fraction.r >= fraction.b) {
      point1 = vec3(high.r, low.g, low.b);
      point2 = vec3(high.r, low.g, high.b);
      first = fraction.r;
      second = fraction.b;
      third = fraction.g;
    } else {
      point1 = vec3(low.r, low.g, high.b);
      point2 = vec3(high.r, low.g, high.b);
      first = fraction.b;
      second = fraction.r;
      third = fraction.g;
    }
  } else if (fraction.b >= fraction.g) {
    point1 = vec3(low.r, low.g, high.b);
    point2 = vec3(low.r, high.g, high.b);
    first = fraction.b;
    second = fraction.g;
    third = fraction.r;
  } else if (fraction.b >= fraction.r) {
    point1 = vec3(low.r, high.g, low.b);
    point2 = vec3(low.r, high.g, high.b);
    first = fraction.g;
    second = fraction.b;
    third = fraction.r;
  } else {
    point1 = vec3(low.r, high.g, low.b);
    point2 = vec3(high.r, high.g, low.b);
    first = fraction.g;
    second = fraction.r;
    third = fraction.b;
  }
  vec3 c1 = lut_point(point1);
  vec3 c2 = lut_point(point2);
  return c000
    + first * (c1 - c000)
    + second * (c2 - c1)
    + third * (c111 - c2);
}

float derby_hash(vec2 point) {
  return fract(sin(dot(point, vec2(12.9898, 78.233))) * 43758.5453);
}

vec3 straight_sample(vec2 uv) {
  vec4 texel = texture(u_texture_input, clamp(uv, vec2(0.0), vec2(1.0)));
  return texel.a > 0.0 ? texel.rgb / texel.a : texel.rgb;
}

// Value noise: smooth clumps one lattice cell across, rather than one speck
// per pixel.
float derby_value_noise(vec2 point) {
  vec2 cell = floor(point);
  vec2 f = fract(point);
  f = f * f * (3.0 - 2.0 * f);
  float a = derby_hash(cell);
  float b = derby_hash(cell + vec2(1.0, 0.0));
  float c = derby_hash(cell + vec2(0.0, 1.0));
  float d = derby_hash(cell + vec2(1.0, 1.0));
  return mix(mix(a, b, f.x), mix(c, d, f.x), f.y) - 0.5;
}

// One layer's grain: clumps the film's size, with finer ones inside them.
float derby_grain_field(vec2 pixel, vec2 offset) {
  vec2 point = pixel / max(u_grain_size, 0.5) + offset;
  return derby_value_noise(point) * 0.8
    + derby_value_noise(point * 2.03 + vec2(13.1, 7.7)) * 0.45;
}

// FILM GRAIN takes its character from the look's film: a fast film's bigger
// clumps, a slide's finer grain, a black-and-white film's lack of colour. It
// shows most in the low mids, thins in the clearest shadows, and nearly
// vanishes toward paper white, as a scanned frame's does.
vec3 apply_film_grain(vec3 color, vec2 pixel, float strength) {
  float luma = dot(color, vec3(0.2126, 0.7152, 0.0722));
  float zone = smoothstep(0.0, 0.16, luma)
    * mix(1.0, 0.25, smoothstep(0.45, 1.0, luma));
  float mono = derby_grain_field(pixel, vec2(0.0));
  vec3 layers = vec3(
    derby_grain_field(pixel, vec2(31.7, 11.3)),
    derby_grain_field(pixel, vec2(-17.9, 43.1)),
    derby_grain_field(pixel, vec2(57.3, -23.9))
  );
  vec3 noise = mix(vec3(mono), layers, u_grain_colour);
  return color + noise * 0.17 * u_grain_amount * strength * zone;
}

// How much light a pixel probably really had. A phone's file stops at white,
// so only the very top of the range is read as the edge of something far
// brighter: a lamp, a candle, the sun on chrome. Skin, a white shirt or an
// ordinary bright sky is not a light source and makes no halo.
float halation_source(vec2 uv) {
  vec3 c = straight_sample(uv);
  float peak = max(c.r, max(c.g, c.b));
  float e = smoothstep(0.80, 1.0, peak);
  return e * e * (1.0 + 2.0 * smoothstep(0.97, 1.0, peak));
}

// GLOW is halation: light that went through the emulsion, bounced off the
// back of the film and came back to expose the red layer (and a little of the
// green) a second time, right around the thing that was bright. It hugs the
// source and fades fast: the taps crowd toward the centre and weigh less as
// they reach out, to about 2% of the frame's short side. The reach is a share
// of the frame, so the preview and the 1440px master halo alike; the taps are
// turned per pixel so the edge breaks into grain rather than rings. Returns
// how much of the halo reaches this pixel; the source itself, already white,
// takes almost none.
float halation_light(vec2 uv, vec3 color) {
  vec2 unit = vec2(min(u_size.x, u_size.y)) / u_size;
  float spin = derby_hash(floor(FlutterFragCoord().xy)) * 6.2831853;
  float halo = 0.0;
  float total = 0.0;
  for (int i = 0; i < 16; i++) {
    float t = (float(i) + 0.5) / 16.0;
    float angle = float(i) * 2.39996323 + spin;
    float weight = exp(-2.5 * t);
    vec2 direction = vec2(cos(angle), sin(angle)) * (0.022 * t * t) * unit;
    halo += halation_source(uv + direction) * weight;
    total += weight;
  }
  float peak = max(color.r, max(color.g, color.b));
  return halo / total * (1.0 - 0.85 * smoothstep(0.80, 1.0, peak));
}

// DIFFUSION is a mist filter in front of the lens: a share of the light from
// every bright thing is scattered into a soft bloom around it, in its own
// colour, which also softens the contrast between a light and its
// surroundings. Unlike halation it is optical, not chemical, so it takes the
// colour of what is bright, reaches further (about 5% of the short side) and
// starts lower in the range: a lit face blooms a little, a window a lot.
vec3 diffusion_light(vec2 uv) {
  vec2 unit = vec2(min(u_size.x, u_size.y)) / u_size;
  float spin = derby_hash(floor(FlutterFragCoord().xy) + vec2(7.0, 3.0))
    * 6.2831853;
  vec3 bloom = vec3(0.0);
  float total = 0.0;
  for (int i = 0; i < 16; i++) {
    float t = (float(i) + 0.5) / 16.0;
    float angle = float(i) * 2.39996323 + spin;
    float weight = exp(-1.6 * t);
    vec2 direction = vec2(cos(angle), sin(angle)) * (0.05 * t) * unit;
    vec3 c = straight_sample(uv + direction);
    float peak = max(c.r, max(c.g, c.b));
    float bright = smoothstep(0.55, 1.0, peak);
    bloom += pow(c, vec3(2.2)) * bright * bright * weight;
    total += weight;
  }
  return bloom / total;
}

// VIGNETTE is the light a lens loses toward the corners, about the cosine to
// the fourth power of the angle off its axis. It is less light reaching the
// film rather than a grey laid over the print, so the look's own shadows do
// the darkening. Level 5 takes the corners down most of a stop, level 10
// nearly two.
float vignette_falloff(vec2 uv, float strength) {
  vec2 offset = (uv - vec2(0.5)) * u_size;
  float d = length(offset) / (0.5 * length(u_size));
  float k = 0.59 * sqrt(strength) * d;
  float c = 1.0 / (1.0 + k * k);
  return c * c;
}

vec3 derby_hsv_to_rgb(float hue, float saturation, float value) {
  vec3 channels = abs(
    fract(vec3(hue) + vec3(0.0, 0.6666667, 0.3333333)) * 6.0 - 3.0
  );
  return value * mix(
    vec3(1.0),
    clamp(channels - 1.0, 0.0, 1.0),
    saturation
  );
}

// LEAK is light that got into the camera through a gap and fogged the film
// before it was developed: it comes in at an edge of the frame, spreads along
// it, and is added as exposure, so the film burns it into its own warm
// colours rather than laying a tint over the picture. Leak's level still
// travels one closed path around the frame, and the seed stored when Leak is
// switched on makes each activation its own leak: where along the edge, how
// far it reaches, and which of film's reds, ambers and magentas it takes.
vec3 leak_light(vec2 uv, float level, float storedSeed) {
  const float tau = 6.28318530718;
  float phase = clamp((level - 1.0) / 9.0, 0.0, 1.0);
  float seed = mod(storedSeed, 104729.0) / 104729.0;
  float angle = 2.35619449019 + tau * phase;
  vec2 heading = vec2(cos(angle), sin(angle));
  // Where that heading leaves the frame, a little outside it.
  float edge = max(abs(heading.x), abs(heading.y));
  vec2 anchor = vec2(0.5) + heading * (0.54 / edge);
  vec2 normal = abs(heading.x) > abs(heading.y)
    ? vec2(sign(heading.x), 0.0)
    : vec2(0.0, sign(heading.y));
  vec2 tangent = vec2(-normal.y, normal.x);
  vec2 aspect = u_size / min(u_size.x, u_size.y);
  vec2 delta = (uv - anchor) * aspect;
  float along = dot(delta, tangent);
  float across = dot(delta, normal);
  float reach = 0.08 + 0.07 * (
    0.5 + 0.5 * sin(tau * phase + seed * 19.7392088)
  );
  float stretch = 2.0 + 1.2 * (
    0.5 + 0.5 * cos(tau * phase + seed * 27.0)
  );
  float r = length(vec2(along / stretch, across));
  float fog = exp(-r / reach) + 0.2 * exp(-r / (reach * 2.6));
  float hue = fract(0.95 + 0.17 * (
    0.5 + 0.5 * sin(tau * phase + seed * 12.5663706)
  ));
  float saturation = 0.72 + 0.18 * (
    0.5 + 0.5 * sin(tau * phase + seed * 43.9822972)
  );
  float intensity = 0.35 + 0.30 * (
    0.5 + 0.5 * cos(tau * phase + seed * 31.4159265)
  );
  vec3 colour = pow(derby_hsv_to_rgb(hue, saturation, 1.0), vec3(2.2));
  return colour * fog * intensity;
}


void main() {
  vec2 uv = FlutterFragCoord().xy / u_size;
#ifdef IMPELLER_TARGET_OPENGLES
  uv.y = 1.0 - uv.y;
#endif
  vec4 source = texture(u_texture_input, uv);
  vec3 straight = source.a > 0.0 ? source.rgb / source.a : source.rgb;

  if (u_effect_diffusion > 0.0 || u_effect_vignette > 0.0 ||
      u_effect_glow > 0.0 || u_effect_leak > 0.0) {
    vec3 light = pow(max(straight, vec3(0.0)), vec3(2.2));
    if (u_effect_diffusion > 0.0) {
      light += diffusion_light(uv) * 0.35 * (u_effect_diffusion / 5.0);
    }
    if (u_effect_vignette > 0.0) {
      light *= vignette_falloff(uv, u_effect_vignette / 5.0);
    }
    if (u_effect_glow > 0.0) {
      light += vec3(1.0, 0.32, 0.10) * halation_light(uv, straight)
        * 0.45 * (u_effect_glow / 5.0);
    }
    if (u_effect_leak > 0.0) {
      light += leak_light(uv, u_effect_leak, u_effect_leak_seed);
    }
    straight = pow(light, vec3(1.0 / 2.2));
  }

  vec3 graded = mix(straight, sample_lut(straight), u_strength);

  if (u_effect_grain > 0.0) {
    vec2 pixel = FlutterFragCoord().xy / max(u_effect_scale, 1.0);
    graded = apply_film_grain(graded, pixel, u_effect_grain / 5.0);
  }

  frag_color = vec4(clamp(graded, 0.0, 1.0) * source.a, source.a);
}
