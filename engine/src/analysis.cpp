#include "analysis.hpp"

#include <algorithm>
#include <cstddef>

namespace pycam
{

namespace
{

// Full range BT.601, in 16.16 fixed point.
constexpr int kRV = 91881; // 1.402
constexpr int kGU = 22554; // 0.344136
constexpr int kGV = 46802; // 0.714136
constexpr int kBU = 116130; // 1.772

inline unsigned bin(int v)
{
	return static_cast<unsigned>(std::clamp(v, 0, 255)) * FrameStats::kBins / 256;
}

} // namespace

FrameStats analyse_yuv420(uint8_t const *frame, unsigned width, unsigned height, unsigned stride, unsigned step)
{
	FrameStats stats;
	if (step == 0)
		step = 1;
	unsigned const uv_stride = stride / 2;
	uint8_t const *u_plane = frame + static_cast<size_t>(stride) * height;
	uint8_t const *v_plane = u_plane + static_cast<size_t>(uv_stride) * (height / 2);
	uint64_t sum = 0, count = 0;
	for (unsigned row = 0; row < height; row += step)
	{
		uint8_t const *line = frame + static_cast<size_t>(row) * stride;
		uint8_t const *u_line = u_plane + static_cast<size_t>(row / 2) * uv_stride;
		uint8_t const *v_line = v_plane + static_cast<size_t>(row / 2) * uv_stride;
		for (unsigned col = 0; col < width; col += step)
		{
			int const y = line[col];
			int const u = u_line[col / 2] - 128, v = v_line[col / 2] - 128;
			stats.luma[y * FrameStats::kBins / 256]++;
			stats.rgb[0][bin(y + ((kRV * v + 32768) >> 16))]++;
			stats.rgb[1][bin(y - ((kGU * u + kGV * v + 32768) >> 16))]++;
			stats.rgb[2][bin(y + ((kBU * u + 32768) >> 16))]++;
			sum += y;
			count++;
		}
	}
	stats.mean = count ? static_cast<float>(sum) / count : 0;
	return stats;
}

} // namespace pycam
