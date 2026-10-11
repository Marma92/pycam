#include "analysis.hpp"

#include <cstddef>

namespace pycam
{

LumaStats analyse_luma(uint8_t const *y, unsigned width, unsigned height, unsigned stride, unsigned step)
{
	LumaStats stats;
	if (step == 0)
		step = 1;
	uint64_t sum = 0, count = 0;
	for (unsigned row = 0; row < height; row += step)
	{
		uint8_t const *line = y + static_cast<size_t>(row) * stride;
		for (unsigned col = 0; col < width; col += step)
		{
			uint8_t v = line[col];
			stats.histogram[v * LumaStats::kBins / 256]++;
			sum += v;
			count++;
		}
	}
	stats.mean = count ? static_cast<float>(sum) / count : 0;
	return stats;
}

} // namespace pycam
