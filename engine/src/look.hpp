#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace pycam
{

// A 3D LUT on the RGB of the "pycam base" (docs/presets.md): full range BT.601 YUV seen as
// RGB 0..255. The application compiles each preset into one; the file format is in
// docs/protocol.md ("look").
struct Lut3D
{
	unsigned size = 0; // points per axis
	std::vector<float> rgb; // size^3 * 3 output values, 0..255; index ((b * size + g) * size + r) * 3

	static std::shared_ptr<const Lut3D> Load(std::string const &path); // throws std::runtime_error
	static std::shared_ptr<const Lut3D> Identity(unsigned size);
};

// What the photos look like, applied to the YUV still before the JPEG is encoded:
// vignetting (the lens), then the LUT (the film), then the grain.
struct Look
{
	std::string name; // for the state and the logs
	std::shared_ptr<const Lut3D> lut; // none: the base
	float vignette = 0; // darkening of the corners, 0..1
	float grain = 0; // 0..1
	float grain_size = 1; // in pixels of the full resolution image

	bool Empty() const { return !lut && vignette <= 0 && grain <= 0; }
};

// In place, on a planar YUV420 frame (chroma planes at half the stride and height, right
// after the luma plane). `seed` changes the grain from one photo to the next.
void apply_look(Look const &look, uint8_t *frame, unsigned width, unsigned height, unsigned stride, uint64_t seed,
				unsigned threads);

} // namespace pycam
