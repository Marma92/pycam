# lj92

Lossless JPEG (ITU-T T.81, SOF3) encoder and decoder by Andrew Baldwin (2014), MIT
licence (notice at the top of `lj92.h`). DNG files use it for lossless raw compression.

Copied from [cinepi-raw](https://github.com/cinepi/cinepi-raw) (`cinepi/lj92.h`, commit
`1f21e099`, 2024-02-12), where it writes the CinemaDNG frames of the CinePI camera. Only
`engine/src/dng_writer.cpp` includes it: the header holds the implementation.

## pycam changes (marked "pycam" in the code)

- `lj92_encode_components()`: interleaved components and predictor 1 or 6. `lj92_encode()`
  is unchanged (1 component, predictor 6).
- Why: darktable's decoder (rawspeed) only accepts predictor 1, and a JPEG frame whose
  size divides the image size. Adobe's layout (2 components, frame W/2 x H, predictor 1)
  satisfies both, and compresses a 12-bit IMX477 frame 2.05x (lj92's original
  one-component predictor 6: 1.40x).
- lj92's own decoder mishandles several components (predictor 6 fast path, shared left
  sample); the DNGs are checked with LibRaw instead (`engine/tests/dng_roundtrip.sh`).
