#pragma once

#include <array>
#include <cstdint>

namespace pycam
{

// Statistics computed on the luma plane of a (small) YUV420 frame.
struct LumaStats
{
	static constexpr unsigned kBins = 64;
	std::array<uint32_t, kBins> histogram {};
	float mean = 0; // 0..255
};

// Samples one pixel every `step` pixels in both directions.
LumaStats analyse_luma(uint8_t const *y, unsigned width, unsigned height, unsigned stride, unsigned step);

} // namespace pycam
