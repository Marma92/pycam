// Checks RawStack and develop() on synthetic raw frames whose result is known (meson test).

#include "../src/develop.hpp"
#include "../src/stack.hpp"

#include <cmath>
#include <cstdio>
#include <vector>

#include <libcamera/formats.h>

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

constexpr unsigned kW = 16, kH = 12;
constexpr unsigned kBlack = 256, kWhite = 4095;

StreamInfo raw_info()
{
	StreamInfo info;
	info.width = kW;
	info.height = kH;
	info.stride = kW * 3 / 2 + 8; // padded rows, as the camera gives them
	info.pixel_format = libcamera::formats::SRGGB12_CSI2P;
	return info;
}

// Packs one uint16 per pixel the way the camera does (CSI-2, 12 bits: 2 pixels in 3 bytes).
std::vector<uint8_t> pack(std::vector<uint16_t> const &pixels, StreamInfo const &info)
{
	std::vector<uint8_t> out(static_cast<size_t>(info.stride) * info.height);
	for (unsigned y = 0; y < info.height; y++)
	{
		uint8_t *row = out.data() + static_cast<size_t>(y) * info.stride;
		for (unsigned x = 0; x < info.width; x += 2)
		{
			uint16_t a = pixels[y * info.width + x], b = pixels[y * info.width + x + 1];
			uint8_t *p = row + x / 2 * 3;
			p[0] = a >> 4;
			p[1] = b >> 4;
			p[2] = (a & 15) | ((b & 15) << 4);
		}
	}
	return out;
}

std::vector<uint16_t> flat(unsigned value)
{
	return std::vector<uint16_t>(kW * kH, value);
}

// A colour per photosite of the RGGB pattern.
std::vector<uint16_t> bayer(unsigned r, unsigned g, unsigned b)
{
	std::vector<uint16_t> out(kW * kH);
	for (unsigned y = 0; y < kH; y++)
		for (unsigned x = 0; x < kW; x++)
			out[y * kW + x] = (y % 2 == 0) ? (x % 2 == 0 ? r : g) : (x % 2 == 0 ? g : b);
	return out;
}

StreamInfo yuv_info(unsigned w, unsigned h)
{
	StreamInfo info;
	info.width = w;
	info.height = h;
	info.stride = w + 4;
	return info;
}

std::vector<uint8_t> linear_tone()
{
	std::vector<uint8_t> tone(256);
	for (unsigned i = 0; i < 256; i++)
		tone[i] = i;
	return tone;
}

DevelopParams params()
{
	DevelopParams p;
	p.black = kBlack;
	p.white = kWhite;
	p.tone = linear_tone();
	return p;
}

void check_stack()
{
	StreamInfo info = raw_info();
	RawStack add(RawStack::Mode::Add), mean(RawStack::Mode::Mean), max(RawStack::Mode::Max);
	for (unsigned v : { 100u, 200u, 300u })
	{
		std::vector<uint8_t> frame = pack(flat(kBlack + v), info);
		add.Add(frame.data(), info, kBlack);
		mean.Add(frame.data(), info, kBlack);
		max.Add(frame.data(), info, kBlack);
	}
	expect(add.frames() == 3, "3 frames");
	expect(add.Take(kWhite)[5] == kBlack + 600, "add: the signals add up, the black level once");
	expect(mean.Take(kWhite)[5] == kBlack + 200, "mean");
	expect(max.Take(kWhite)[5] == kBlack + 300, "max");

	RawStack bright(RawStack::Mode::Add);
	std::vector<uint8_t> frame = pack(flat(3000), info);
	bright.Add(frame.data(), info, kBlack);
	bright.Add(frame.data(), info, kBlack);
	expect(bright.Take(kWhite)[0] == kWhite, "add: saturates like the sensor");

	// The noise below black averages out instead of being clipped frame by frame.
	RawStack noisy(RawStack::Mode::Add);
	for (unsigned v : { kBlack - 10, kBlack + 10 })
	{
		std::vector<uint8_t> f = pack(flat(v), info);
		noisy.Add(f.data(), info, kBlack);
	}
	expect(noisy.Take(kWhite)[3] == kBlack, "add: noise around black stays black");
}

void check_develop()
{
	// A grey: the same signal under each colour, white balance and matrix neutral.
	std::vector<uint16_t> raw = flat(kBlack + 1000);
	StreamInfo out_info = yuv_info(kW, kH);
	std::vector<uint8_t> yuv(out_info.stride * kH * 3 / 2);
	develop(raw, kW, kH, params(), yuv.data(), out_info, 2);
	unsigned const expected = static_cast<unsigned>(1000.0f / (kWhite - kBlack) * 255 + 0.5f);
	bool grey = true;
	for (unsigned y = 0; y < kH; y++)
		for (unsigned x = 0; x < kW; x++)
			grey &= std::abs(int(yuv[y * out_info.stride + x]) - int(expected)) <= 1;
	uint8_t const *u = yuv.data() + out_info.stride * kH, *v = u + (out_info.stride / 2) * (kH / 2);
	for (unsigned i = 0; i < kW / 2; i++)
		grey &= std::abs(u[i] - 128) <= 1 && std::abs(v[i] - 128) <= 1;
	expect(grey, "a grey stays grey, edges included");

	// Pure red: demosaicing gives red everywhere and nothing else.
	raw = bayer(kBlack + 2000, kBlack, kBlack);
	develop(raw, kW, kH, params(), yuv.data(), out_info, 3);
	unsigned const red = static_cast<unsigned>(2000.0f / (kWhite - kBlack) * 255 + 0.5f);
	int const y_red = static_cast<int>(0.299f * red + 0.5f);
	int const cr_red = static_cast<int>(128 + 0.5f * red + 0.5f);
	expect(std::abs(yuv[5 * out_info.stride + 7] - y_red) <= 1, "red: its luma");
	expect(std::abs(v[3] - cr_red) <= 1, "red: its chroma");

	// White balance gains and the colour matrix: a blue cast corrected to grey.
	raw = bayer(kBlack + 500, kBlack + 1000, kBlack + 250);
	DevelopParams balanced = params();
	balanced.gain_r = 2;
	balanced.gain_b = 4;
	develop(raw, kW, kH, balanced, yuv.data(), out_info, 1);
	expect(std::abs(u[2] - 128) <= 1 && std::abs(v[2] - 128) <= 1, "gains: grey again");

	// A highlight where green saturates first: white, not magenta through a matrix that
	// takes green away from red and blue.
	raw = bayer(kBlack + 2600, kWhite, kBlack + 2200);
	DevelopParams saturated = params();
	saturated.gain_r = 1.8;
	saturated.gain_b = 2.0;
	saturated.ccm = { 1.8f, -0.6f, -0.2f, -0.3f, 1.8f, -0.5f, -0.1f, -0.6f, 1.7f };
	develop(raw, kW, kH, saturated, yuv.data(), out_info, 1);
	expect(yuv[5 * out_info.stride + 7] >= 250 && std::abs(u[2] - 128) <= 2 && std::abs(v[2] - 128) <= 2,
		   "a saturated highlight stays white");

	// A hot pixel is replaced; the same pixel in a bright area is left alone.
	raw = flat(kBlack + 20);
	raw[6 * kW + 8] = kBlack + 3000;
	develop(raw, kW, kH, params(), yuv.data(), out_info, 1);
	expect(raw[6 * kW + 8] < kBlack + 100, "a hot pixel is fixed");
	raw = flat(kBlack + 1500);
	raw[6 * kW + 8] = kBlack + 3000;
	develop(raw, kW, kH, params(), yuv.data(), out_info, 1);
	expect(raw[6 * kW + 8] == kBlack + 3000, "a highlight is not a hot pixel");

	// Smaller output: the grey again, from a halved image.
	raw = flat(kBlack + 1000);
	StreamInfo half = yuv_info(kW / 2, kH / 2);
	std::vector<uint8_t> small(half.stride * half.height * 3 / 2);
	develop(raw, kW, kH, params(), small.data(), half, 2);
	expect(std::abs(int(small[2 * half.stride + 3]) - int(expected)) <= 1, "scaled down");
}

void check_tone()
{
	std::vector<uint8_t> tone = tone_curve("/nonexistent.json", 1001);
	expect(tone.front() == 0 && tone.back() == 255, "Rec.709 by default: ends");
	expect(std::abs(tone[180] - 104) <= 1, "Rec.709: 18 % grey at 41 %");
}

} // namespace

int main()
{
	check_stack();
	check_develop();
	check_tone();
	if (failures)
		return 1;
	std::printf("stack: all checks passed\n");
	return 0;
}
