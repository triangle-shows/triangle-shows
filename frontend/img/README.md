# Poster background assets

## ripple-matrix.jpg

The water-ripple field the lineup poster can draw as a two-tone dot matrix
(`frontend/js/lineup.js`, the `photo` background).

**Source:** ["Duck With Rings"](https://commons.wikimedia.org/wiki/File:Duck_With_Rings_(Unsplash).jpg)
by Gary Bendig ([@kris_ricepees](https://unsplash.com/@kris_ricepees)), via Unsplash and
Wikimedia Commons. **Licence: CC0 1.0** (public domain dedication) — no attribution is
required, and this note exists to record that the file is ours to ship.

**What was done to it:** cropped to a duck-free region left of and below the ring centre
— the window is `(30, 1500, 1680, 3100)` in the 5184x3456 original, which stops short of
the duck's left edge at about x=1730; an earlier window ran to x=1900 and clipped ~170px
of bird, which the half turn below then deposited at the left edge of the asset —
converted to greyscale; high-pass filtered (the original minus a blurred copy) so what
survives is the ring modulation rather than the scene's lighting, which would otherwise
ink one corner solid and leave another bare; histogram-equalised; lightly blurred to drop
the grain the high-pass lifted, which cost file size and speckled the dither without
adding structure; and rotated 180° so the rings appear to spread from the bottom-left
corner of the page.

It is deliberately small — 420x296. The matrix samples roughly 271x183 cells, so this has
headroom to spare and the dither hides JPEG artefacts entirely.

Regenerate by re-running the crop-and-prepare step against the Commons original; the
parameters above are the whole recipe.
