#pragma once

#include <array>
#include <cstdint>

namespace pycam
{

// Statistics computed on a (small) YUV420 frame, planar (Y, then U, then V), full range
// BT.601 (sYCC: the colour space of the lores and viewfinder streams).
struct FrameStats
{
	static constexpr unsigned kBins = 64;
	using Histogram = std::array<uint32_t, kBins>;
	Histogram luma {};
	std::array<Histogram, 3> rgb {}; // red, green, blue
	float mean = 0; // of the luma, 0..255
};

// Samples one pixel every `step` pixels in both directions; each sample takes the chroma
// of its 2x2 block.
FrameStats analyse_yuv420(uint8_t const *frame, unsigned width, unsigned height, unsigned stride, unsigned step);

} // namespace pycam
