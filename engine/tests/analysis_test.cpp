// Checks analyse_yuv420 on synthetic frames whose colours are known (meson test).

#include "../src/analysis.hpp"

#include <algorithm>
#include <cstdio>
#include <vector>

using namespace pycam;

namespace
{

int failures = 0;

void expect(bool ok, char const *what)
{
	if (!ok)
	{
		std::fprintf(stderr, "FAIL: %s\n", what);
		failures++;
	}
}

// A frame of one colour, with a stride wider than the image as the ISP gives.
std::vector<uint8_t> uniform(unsigned width, unsigned height, unsigned stride, uint8_t y, uint8_t u, uint8_t v)
{
	std::vector<uint8_t> frame(stride * height + 2 * (stride / 2) * (height / 2));
	for (unsigned row = 0; row < height; row++)
		std::fill_n(frame.begin() + row * stride, width, y);
	auto u_plane = frame.begin() + stride * height, v_plane = u_plane + (stride / 2) * (height / 2);
	for (unsigned row = 0; row < height / 2; row++)
	{
		std::fill_n(u_plane + row * (stride / 2), width / 2, u);
		std::fill_n(v_plane + row * (stride / 2), width / 2, v);
	}
	return frame;
}

unsigned peak(FrameStats::Histogram const &h)
{
	unsigned best = 0;
	for (unsigned i = 1; i < h.size(); i++)
		if (h[i] > h[best])
			best = i;
	return best;
}

} // namespace

int main()
{
	unsigned const w = 64, h = 48, stride = 80, step = 2, samples = (w / step) * (h / step);

	// Grey: the three channels and the luma in the same bin.
	auto grey = uniform(w, h, stride, 128, 128, 128);
	FrameStats s = analyse_yuv420(grey.data(), w, h, stride, step);
	expect(s.luma[32] == samples, "grey luma");
	expect(s.rgb[0][32] == samples && s.rgb[1][32] == samples && s.rgb[2][32] == samples, "grey rgb");
	expect(s.mean == 128, "grey mean");

	// Pure red in full range BT.601: Y 76, U 85, V 255 -> R 254, G 0, B 0.
	auto red = uniform(w, h, stride, 76, 85, 255);
	s = analyse_yuv420(red.data(), w, h, stride, step);
	expect(peak(s.rgb[0]) == 63 && peak(s.rgb[1]) == 0 && peak(s.rgb[2]) == 0, "red");
	expect(peak(s.luma) == 76 * 64 / 256, "red luma");

	// Pure blue: Y 29, U 255, V 107 -> R 0, G 0, B 254.
	auto blue = uniform(w, h, stride, 29, 255, 107);
	s = analyse_yuv420(blue.data(), w, h, stride, step);
	expect(peak(s.rgb[0]) == 0 && peak(s.rgb[1]) == 0 && peak(s.rgb[2]) == 63, "blue");

	// The padding past the width is never read: fill it with white, nothing changes.
	for (unsigned row = 0; row < h; row++)
		std::fill_n(grey.begin() + row * stride + w, stride - w, 255);
	s = analyse_yuv420(grey.data(), w, h, stride, step);
	expect(s.luma[63] == 0, "padding");

	if (!failures)
		std::printf("analysis: ok\n");
	return failures ? 1 : 0;
}
