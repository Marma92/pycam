// Checks apply_look and the LUT file on synthetic frames (meson test).

#include "../src/look.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <random>
#include <stdexcept>
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

unsigned const w = 64, h = 48, stride = 80;
size_t const frame_size = size_t(stride) * h + 2 * size_t(stride / 2) * (h / 2);

// Moderate colours: no channel clips, so YUV -> RGB -> YUV is exact.
std::vector<uint8_t> random_frame(unsigned seed)
{
	std::mt19937 rng(seed);
	std::vector<uint8_t> frame(frame_size, 0);
	for (unsigned y = 0; y < h; y++)
		for (unsigned x = 0; x < w; x++)
			frame[y * stride + x] = 60 + rng() % 120;
	uint8_t *u = frame.data() + stride * h, *v = u + (stride / 2) * (h / 2);
	for (unsigned y = 0; y < h / 2; y++)
		for (unsigned x = 0; x < w / 2; x++)
			u[y * (stride / 2) + x] = 118 + rng() % 20, v[y * (stride / 2) + x] = 118 + rng() % 20;
	return frame;
}

int max_difference(std::vector<uint8_t> const &a, std::vector<uint8_t> const &b)
{
	int worst = 0;
	for (size_t i = 0; i < a.size(); i++)
		worst = std::max(worst, std::abs(int(a[i]) - int(b[i])));
	return worst;
}

double mean_luma(std::vector<uint8_t> const &frame, unsigned x0, unsigned y0, unsigned x1, unsigned y1)
{
	double sum = 0;
	for (unsigned y = y0; y < y1; y++)
		for (unsigned x = x0; x < x1; x++)
			sum += frame[y * stride + x];
	return sum / ((x1 - x0) * (y1 - y0));
}

} // namespace

int main()
{
	// An identity LUT changes nothing (but rounding), whatever the number of threads.
	auto original = random_frame(1);
	for (unsigned threads : { 1u, 3u })
	{
		auto frame = original;
		Look look;
		look.lut = Lut3D::Identity(33);
		apply_look(look, frame.data(), w, h, stride, 1, threads);
		expect(max_difference(frame, original) <= 1, "identity LUT");
	}

	// A constant LUT: every pixel gets its colour, (200, 50, 50) -> Y 95, U 103, V 203.
	{
		auto lut = std::make_shared<Lut3D>(*Lut3D::Identity(2));
		for (size_t i = 0; i < lut->rgb.size(); i += 3)
			lut->rgb[i] = 200, lut->rgb[i + 1] = 50, lut->rgb[i + 2] = 50;
		Look look;
		look.lut = lut;
		auto frame = original;
		apply_look(look, frame.data(), w, h, stride, 1, 2);
		uint8_t const *u = frame.data() + stride * h, *v = u + (stride / 2) * (h / 2);
		expect(frame[0] == 95 && frame[(h - 1) * stride + w - 1] == 95, "constant LUT: luma");
		expect(u[0] == 103 && v[0] == 203, "constant LUT: chroma");
		expect(frame[w] == original[w], "the padding past the width is left alone");
	}

	// Vignetting: the corners darker, the centre untouched.
	{
		std::vector<uint8_t> frame(frame_size, 128);
		Look look;
		look.vignette = 0.5f;
		apply_look(look, frame.data(), w, h, stride, 1, 1);
		double const centre = mean_luma(frame, w / 2 - 4, h / 2 - 4, w / 2 + 4, h / 2 + 4);
		double const corner = mean_luma(frame, 0, 0, 4, 4);
		expect(std::abs(centre - 128) < 1, "vignetting: centre");
		expect(corner < 128 * 0.7, "vignetting: corners");
	}

	// Grain: the same seed gives the same grain, another seed another one; the mean stays.
	{
		std::vector<uint8_t> grey(frame_size, 128);
		Look look;
		look.grain = 0.5f;
		look.grain_size = 1.5f;
		auto a = grey, b = grey, c = grey;
		apply_look(look, a.data(), w, h, stride, 7, 2);
		apply_look(look, b.data(), w, h, stride, 7, 3);
		apply_look(look, c.data(), w, h, stride, 8, 2);
		expect(a == b, "grain: deterministic");
		expect(a != c, "grain: changes with the seed");
		expect(std::abs(mean_luma(a, 0, 0, w, h) - 128) < 2, "grain: mean kept");
		expect(max_difference(a, grey) > 3, "grain: visible");
	}

	// Film grain: colour (chroma) grain only when asked, more in the shadows when asked,
	// clumps change the pattern, still reproducible.
	{
		auto chroma_moved = [](std::vector<uint8_t> const &frame)
		{
			uint8_t const *u = frame.data() + stride * h;
			for (size_t i = 0; i < 2 * size_t(stride / 2) * (h / 2); i++)
				if (u[i] != 128)
					return true;
			return false;
		};
		std::vector<uint8_t> grey(frame_size, 128);
		Look mono;
		mono.grain = 0.6f;
		auto a = grey;
		apply_look(mono, a.data(), w, h, stride, 3, 2);
		expect(!chroma_moved(a), "grain: black and white leaves the chroma");
		Look colour = mono;
		colour.grain_colour = 1;
		auto b = grey;
		apply_look(colour, b.data(), w, h, stride, 3, 2);
		expect(chroma_moved(b), "grain: colour grain in the chroma");

		auto spread = [](std::vector<uint8_t> const &frame, int level)
		{
			double sum = 0;
			for (unsigned y = 0; y < h; y++)
				for (unsigned x = 0; x < w; x++)
					sum += std::abs(frame[y * stride + x] - level);
			return sum / (w * h);
		};
		std::vector<uint8_t> dark(frame_size, 128);
		std::fill_n(dark.begin(), stride * h, 70);
		Look mids = mono, shadows = mono;
		shadows.grain_shadows = 1;
		auto c = dark, d = dark;
		apply_look(mids, c.data(), w, h, stride, 3, 2);
		apply_look(shadows, d.data(), w, h, stride, 3, 2);
		expect(spread(d, 70) > spread(c, 70) * 1.05, "grain: more in the shadows when asked");

		Look clumpy = mono;
		clumpy.grain_clumps = 0.5f;
		auto e = grey, f = grey;
		apply_look(clumpy, e.data(), w, h, stride, 3, 2);
		apply_look(clumpy, f.data(), w, h, stride, 3, 3);
		expect(e == f && e != a, "grain: clumps, reproducible");
	}

	// The viewfinder preview.
	{
		Preview identity;
		for (int i = 0; i < 256; i++)
			identity.luma[i] = static_cast<uint8_t>(i);
		auto frame = original;
		apply_preview(identity, frame.data(), w, h, stride);
		expect(frame == original, "preview: identity");

		Preview grey = identity; // a negative, in black and white
		for (int i = 0; i < 256; i++)
			grey.luma[i] = static_cast<uint8_t>(255 - i);
		grey.chroma = { 0, 0, 0, 0 };
		apply_preview(grey, frame.data(), w, h, stride);
		uint8_t const *u = frame.data() + stride * h, *v = u + (stride / 2) * (h / 2);
		expect(frame[0] == 255 - original[0] && u[0] == 128 && v[0] == 128, "preview: curve and chroma");

		Preview red_filter = identity; // the reds lighter: luma + Cr
		red_filter.mix_cr = 1;
		frame = original;
		apply_preview(red_filter, frame.data(), w, h, stride);
		int const cr = original[stride * h + (stride / 2) * (h / 2)] - 128;
		expect(frame[0] == std::clamp(original[0] + cr, 0, 255), "preview: luma mix");
	}

	// The LUT file: what the application writes (docs/protocol.md, "look").
	{
		char const *path = "look-test.lut";
		{
			std::ofstream file(path, std::ios::binary);
			uint32_t header[2] = { 1, 2 };
			file.write("PYCAMLUT", 8);
			file.write(reinterpret_cast<char const *>(header), sizeof(header));
			for (unsigned b = 0; b < 2; b++)
				for (unsigned g = 0; g < 2; g++)
					for (unsigned r = 0; r < 2; r++)
					{
						uint16_t rgb[3] = { uint16_t(r * 65535), uint16_t(g * 65535), uint16_t(b * 65535) };
						file.write(reinterpret_cast<char const *>(rgb), sizeof(rgb));
					}
		}
		auto lut = Lut3D::Load(path);
		expect(lut->size == 2 && lut->rgb == Lut3D::Identity(2)->rgb, "LUT file");
		{
			std::ofstream file(path, std::ios::binary | std::ios::app);
			file.put(0);
		}
		bool refused = false;
		try
		{
			Lut3D::Load(path);
		}
		catch (std::runtime_error const &)
		{
			refused = true;
		}
		expect(refused, "LUT file: wrong size refused");
		std::remove(path);
	}

	if (!failures)
		std::printf("look: ok\n");
	return failures ? 1 : 0;
}
