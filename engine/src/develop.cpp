#include "develop.hpp"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <iostream>
#include <thread>

#include <libcamera/control_ids.h>
#include <nlohmann/json.hpp>

#include "dng_writer.hpp"

namespace controls = libcamera::controls;

namespace pycam
{

namespace
{

// Colour of each photosite by Bayer order, indexed by (y & 1) * 2 + (x & 1): 0 R, 1 G, 2 B.
constexpr int kCfa[4][4] = {
	{ 0, 1, 1, 2 }, // RGGB
	{ 1, 0, 2, 1 }, // GRBG
	{ 1, 2, 0, 1 }, // GBRG
	{ 2, 1, 1, 0 }, // BGGR
};

float rec709(float x)
{
	return x < 0.018f ? 4.5f * x : 1.099f * std::pow(x, 0.45f) - 0.099f;
}

// Mirrored around the edges, by an even offset so that the Bayer pattern is kept.
inline int reflect(int i, int n)
{
	if (i < 0)
		return -i;
	if (i >= n)
		return 2 * (n - 1) - i;
	return i;
}

// A hot pixel stands alone: far above its neighbours of the same colour (2 away) and
// above all the pixels around it, which a star or a highlight lights too.
void fix_hot_pixels(std::vector<uint16_t> &raw, unsigned width, unsigned height, int black)
{
	constexpr int kThreshold = 64; // raw units, above 4 times the neighbours
	int const w = static_cast<int>(width), h = static_cast<int>(height);
	for (int y = 2; y + 2 < h; y++)
	{
		uint16_t *row = raw.data() + static_cast<size_t>(y) * width;
		for (int x = 2; x + 2 < w; x++)
		{
			int const s = row[x] - black;
			if (s <= kThreshold)
				continue;
			int const same[4] = { row[x - 2] - black, row[x + 2] - black, row[x - 2 * w] - black, row[x + 2 * w] - black };
			int around = std::max({ same[0], same[1], same[2], same[3] });
			for (int dy = -1; dy <= 1; dy++)
				for (int dx = -1; dx <= 1; dx++)
					if (dx || dy)
						around = std::max(around, row[dy * w + dx + x] - black);
			if (s > 4 * std::max(around, 0) + kThreshold)
				row[x] = static_cast<uint16_t>(std::max(0, (same[0] + same[1] + same[2] + same[3]) / 4 + black));
		}
	}
}

struct Developer
{
	uint16_t const *raw;
	int width, height;
	DevelopParams const &p;
	float scale[3]; // camera RGB minus black -> 0..1, with the gains
	float tone_steps;

	// Malvar-He-Cutler demosaicing, its kernels times 16 (they add up to 16, so the
	// black level goes through them unchanged and is subtracted after).
	void Pixel(int x, int y, uint16_t const *const rows[5], bool inside, uint8_t out[3]) const
	{
		auto at = [&](int dx, int dy) -> int
		{
			uint16_t const *row = rows[dy + 2];
			return row[inside ? x + dx : reflect(x + dx, width)];
		};
		int const *cfa = kCfa[p.bayer_order];
		int const c = cfa[(y & 1) * 2 + (x & 1)];
		int const centre = at(0, 0);
		int rgb[3];
		if (c != 1)
		{
			// Green: centre 8, the 4 greens 4, the 4 same colours 2 away -2.
			int const g = 8 * centre + 4 * (at(-1, 0) + at(1, 0) + at(0, -1) + at(0, 1)) -
						  2 * (at(-2, 0) + at(2, 0) + at(0, -2) + at(0, 2));
			// The other colour, on the diagonals: centre 12, diagonals 4, same colour 2 away -3.
			int const other = 12 * centre + 4 * (at(-1, -1) + at(1, -1) + at(-1, 1) + at(1, 1)) -
							  3 * (at(-2, 0) + at(2, 0) + at(0, -2) + at(0, 2));
			rgb[c] = 16 * centre;
			rgb[1] = g;
			rgb[2 - c] = other;
		}
		else
		{
			// The colour of the left and right neighbours: centre 10, them 8, greens 2
			// away on the row -2, on the column +1, the 4 diagonal greens -2.
			int const diagonals = at(-1, -1) + at(1, -1) + at(-1, 1) + at(1, 1);
			int const across = 10 * centre + 8 * (at(-1, 0) + at(1, 0)) - 2 * (at(-2, 0) + at(2, 0)) +
							   (at(0, -2) + at(0, 2)) - 2 * diagonals;
			// The colour above and below: the same, turned a quarter.
			int const along = 10 * centre + 8 * (at(0, -1) + at(0, 1)) - 2 * (at(0, -2) + at(0, 2)) +
							  (at(-2, 0) + at(2, 0)) - 2 * diagonals;
			int const side = cfa[(y & 1) * 2 + ((x + 1) & 1)];
			rgb[1] = 16 * centre;
			rgb[side] = across;
			rgb[2 - side] = along;
		}

		// Clipped after the gains, as the ISP does: where the sensor saturates (green
		// first), the other colours stop there too and the highlight stays white instead
		// of turning magenta through the colour matrix.
		float cam[3];
		for (int i = 0; i < 3; i++)
			cam[i] = std::min((rgb[i] * (1.0f / 16) - p.black) * scale[i], 1.0f);
		auto const &m = p.ccm;
		for (int i = 0; i < 3; i++)
		{
			float v = m[3 * i] * cam[0] + m[3 * i + 1] * cam[1] + m[3 * i + 2] * cam[2];
			v = std::clamp(v, 0.0f, 1.0f);
			out[i] = p.tone[static_cast<size_t>(v * tone_steps + 0.5f)];
		}
	}

	// Rows [y0, y1) (even), into a full resolution YUV 4:2:0 image.
	void Rows(int y0, int y1, uint8_t *yuv, unsigned stride) const
	{
		uint8_t *const u_plane = yuv + static_cast<size_t>(stride) * height;
		uint8_t *const v_plane = u_plane + static_cast<size_t>(stride / 2) * (height / 2);
		std::vector<uint8_t> rgb(static_cast<size_t>(width) * 6); // two rows
		for (int y = y0; y < y1; y += 2)
		{
			for (int r = 0; r < 2 && y + r < height; r++)
			{
				int const yy = y + r;
				uint16_t const *rows[5];
				for (int k = 0; k < 5; k++)
					rows[k] = raw + static_cast<size_t>(reflect(yy + k - 2, height)) * width;
				bool const rows_inside = yy >= 2 && yy + 2 < height;
				uint8_t *out = rgb.data() + static_cast<size_t>(r) * width * 3;
				for (int x = 0; x < width; x++)
					Pixel(x, yy, rows, rows_inside && x >= 2 && x + 2 < width, out + 3 * x);
				uint8_t *luma = yuv + static_cast<size_t>(yy) * stride;
				for (int x = 0; x < width; x++)
				{
					uint8_t const *c = out + 3 * x;
					luma[x] = static_cast<uint8_t>(0.299f * c[0] + 0.587f * c[1] + 0.114f * c[2] + 0.5f);
				}
			}
			uint8_t *u = u_plane + static_cast<size_t>(y / 2) * (stride / 2);
			uint8_t *v = v_plane + static_cast<size_t>(y / 2) * (stride / 2);
			int const next = y + 1 < height ? width * 3 : 0;
			for (int x = 0; x + 1 < width; x += 2)
			{
				float sum[3];
				for (int i = 0; i < 3; i++)
				{
					uint8_t const *c = rgb.data() + 3 * x + i;
					sum[i] = (c[0] + c[3] + c[next] + c[next + 3]) * 0.25f;
				}
				float const cb = 128.0f - 0.168736f * sum[0] - 0.331264f * sum[1] + 0.5f * sum[2];
				float const cr = 128.0f + 0.5f * sum[0] - 0.418688f * sum[1] - 0.081312f * sum[2];
				u[x / 2] = static_cast<uint8_t>(std::clamp(cb + 0.5f, 0.0f, 255.0f));
				v[x / 2] = static_cast<uint8_t>(std::clamp(cr + 0.5f, 0.0f, 255.0f));
			}
		}
	}
};

// A YUV 4:2:0 plane scaled down by averaging the source pixels each output pixel covers.
void shrink_plane(uint8_t const *src, unsigned sw, unsigned sh, unsigned sstride, uint8_t *dst, unsigned dw,
				  unsigned dh, unsigned dstride)
{
	for (unsigned y = 0; y < dh; y++)
	{
		unsigned const y0 = y * sh / dh, y1 = std::max(y0 + 1, (y + 1) * sh / dh);
		for (unsigned x = 0; x < dw; x++)
		{
			unsigned const x0 = x * sw / dw, x1 = std::max(x0 + 1, (x + 1) * sw / dw);
			unsigned sum = 0;
			for (unsigned j = y0; j < y1; j++)
				for (unsigned i = x0; i < x1; i++)
					sum += src[static_cast<size_t>(j) * sstride + i];
			unsigned const n = (y1 - y0) * (x1 - x0);
			dst[static_cast<size_t>(y) * dstride + x] = static_cast<uint8_t>((sum + n / 2) / n);
		}
	}
}

} // namespace

std::vector<uint8_t> tone_curve(std::string const &tuning_file, size_t size)
{
	std::vector<float> xs, ys;
	try
	{
		std::ifstream in(tuning_file);
		if (in)
		{
			nlohmann::json tuning = nlohmann::json::parse(in);
			for (auto const &algorithm : tuning.at("algorithms"))
				if (algorithm.contains("rpi.contrast"))
				{
					std::vector<float> curve = algorithm["rpi.contrast"].at("gamma_curve").get<std::vector<float>>();
					for (size_t i = 0; i + 1 < curve.size(); i += 2)
					{
						xs.push_back(curve[i] / 65535.0f);
						ys.push_back(curve[i + 1] / 65535.0f);
					}
				}
		}
	}
	catch (std::exception const &e)
	{
		std::cerr << "pycam-engine: no tone curve in " << tuning_file << " (" << e.what() << "), using Rec.709\n";
		xs.clear();
	}

	std::vector<uint8_t> table(size);
	for (size_t i = 0; i < size; i++)
	{
		float const x = static_cast<float>(i) / (size - 1);
		float y;
		if (xs.size() < 2)
			y = rec709(x);
		else
		{
			auto it = std::upper_bound(xs.begin(), xs.end(), x);
			if (it == xs.begin())
				y = ys.front();
			else if (it == xs.end())
				y = ys.back();
			else
			{
				size_t const k = it - xs.begin();
				float const t = (x - xs[k - 1]) / (xs[k] - xs[k - 1]);
				y = ys[k - 1] + t * (ys[k] - ys[k - 1]);
			}
		}
		table[i] = static_cast<uint8_t>(std::clamp(y * 255.0f + 0.5f, 0.0f, 255.0f));
	}
	return table;
}

DevelopParams develop_params(libcamera::ControlList const &metadata, StreamInfo const &raw_info)
{
	DevelopParams p;
	int const bits = raw_bits(raw_info);
	p.bayer_order = std::max(0, raw_bayer_order(raw_info));
	p.white = static_cast<float>((1 << bits) - 1);
	p.black = 4096.0f * (1 << bits) / 65536.0f;
	if (auto levels = metadata.get(controls::SensorBlackLevels))
		p.black = (*levels)[0] * (1 << bits) / 65536.0f;
	if (auto gains = metadata.get(controls::ColourGains))
	{
		p.gain_r = (*gains)[0];
		p.gain_b = (*gains)[1];
	}
	if (auto gain = metadata.get(controls::DigitalGain))
		p.digital_gain = *gain;
	if (auto ccm = metadata.get(controls::ColourCorrectionMatrix))
		std::copy(ccm->begin(), ccm->end(), p.ccm.begin());
	return p;
}

void develop(std::vector<uint16_t> &raw, unsigned width, unsigned height, DevelopParams const &params, uint8_t *yuv,
			 StreamInfo const &yuv_info, unsigned threads)
{
	if (params.tone.size() < 2)
		throw std::invalid_argument("develop: no tone curve");
	if (raw.size() < static_cast<size_t>(width) * height || width < 4 || height < 4)
		throw std::invalid_argument("develop: bad raw size");
	if (params.hot_pixels)
		fix_hot_pixels(raw, width, height, static_cast<int>(params.black + 0.5f));

	float const range = params.white - params.black;
	Developer developer { raw.data(),
						  static_cast<int>(width),
						  static_cast<int>(height),
						  params,
						  { params.gain_r * params.digital_gain / range, params.digital_gain / range,
							params.gain_b * params.digital_gain / range },
						  static_cast<float>(params.tone.size() - 1) };

	// At full size straight into the output; smaller, through a full size image.
	bool const full = yuv_info.width == width && yuv_info.height == height;
	std::vector<uint8_t> temp;
	uint8_t *out = yuv;
	unsigned stride = yuv_info.stride;
	if (!full)
	{
		stride = width;
		temp.resize(static_cast<size_t>(width) * height * 3 / 2);
		out = temp.data();
	}

	threads = std::max(1u, threads);
	std::vector<std::thread> workers;
	int const pairs = (static_cast<int>(height) + 1) / 2;
	for (unsigned t = 0; t < threads; t++)
	{
		int const y0 = 2 * (pairs * static_cast<int>(t) / static_cast<int>(threads));
		int const y1 = std::min(static_cast<int>(height), 2 * (pairs * static_cast<int>(t + 1) / static_cast<int>(threads)));
		workers.emplace_back([&, y0, y1] { developer.Rows(y0, y1, out, stride); });
	}
	for (auto &w : workers)
		w.join();

	if (!full)
	{
		unsigned const dw = yuv_info.width, dh = yuv_info.height, ds = yuv_info.stride;
		shrink_plane(out, width, height, stride, yuv, dw, dh, ds);
		uint8_t const *su = out + static_cast<size_t>(stride) * height;
		uint8_t const *sv = su + static_cast<size_t>(stride / 2) * (height / 2);
		uint8_t *du = yuv + static_cast<size_t>(ds) * dh;
		uint8_t *dv = du + static_cast<size_t>(ds / 2) * (dh / 2);
		shrink_plane(su, width / 2, height / 2, stride / 2, du, dw / 2, dh / 2, ds / 2);
		shrink_plane(sv, width / 2, height / 2, stride / 2, dv, dw / 2, dh / 2, ds / 2);
	}
}

} // namespace pycam
