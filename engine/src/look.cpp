#include "look.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <thread>

namespace pycam
{

namespace
{

constexpr char kMagic[8] = { 'P', 'Y', 'C', 'A', 'M', 'L', 'U', 'T' };
constexpr uint32_t kVersion = 1;

// Full range BT.601, as the rest of the engine (analysis.cpp, focus_aids.cpp).
constexpr float kRV = 1.402f, kGU = 0.344136f, kGV = 0.714136f, kBU = 1.772f;

inline uint8_t to_u8(float v)
{
	return static_cast<uint8_t>(std::clamp(static_cast<int>(v + 0.5f), 0, 255));
}

// Tetrahedral interpolation, input and output 0..255.
inline void lut_lookup(Lut3D const &lut, float r, float g, float b, float out[3])
{
	unsigned const n = lut.size;
	float const s = (n - 1) / 255.0f;
	r = std::clamp(r, 0.0f, 255.0f) * s;
	g = std::clamp(g, 0.0f, 255.0f) * s;
	b = std::clamp(b, 0.0f, 255.0f) * s;
	unsigned const r0 = std::min(static_cast<unsigned>(r), n - 2), g0 = std::min(static_cast<unsigned>(g), n - 2),
				   b0 = std::min(static_cast<unsigned>(b), n - 2);
	float const fr = r - r0, fg = g - g0, fb = b - b0;
	auto at = [&](unsigned dr, unsigned dg, unsigned db)
	{ return &lut.rgb[((static_cast<size_t>(b0 + db) * n + g0 + dg) * n + r0 + dr) * 3]; };
	float const *c000 = at(0, 0, 0), *c111 = at(1, 1, 1), *c1, *c2;
	float w0, w1, w2, w3;
	if (fr > fg)
	{
		if (fg > fb)
			c1 = at(1, 0, 0), c2 = at(1, 1, 0), w0 = 1 - fr, w1 = fr - fg, w2 = fg - fb, w3 = fb;
		else if (fr > fb)
			c1 = at(1, 0, 0), c2 = at(1, 0, 1), w0 = 1 - fr, w1 = fr - fb, w2 = fb - fg, w3 = fg;
		else
			c1 = at(0, 0, 1), c2 = at(1, 0, 1), w0 = 1 - fb, w1 = fb - fr, w2 = fr - fg, w3 = fg;
	}
	else
	{
		if (fb > fg)
			c1 = at(0, 0, 1), c2 = at(0, 1, 1), w0 = 1 - fb, w1 = fb - fg, w2 = fg - fr, w3 = fr;
		else if (fb > fr)
			c1 = at(0, 1, 0), c2 = at(0, 1, 1), w0 = 1 - fg, w1 = fg - fb, w2 = fb - fr, w3 = fr;
		else
			c1 = at(0, 1, 0), c2 = at(1, 1, 0), w0 = 1 - fg, w1 = fg - fr, w2 = fr - fb, w3 = fb;
	}
	for (int k = 0; k < 3; k++)
		out[k] = w0 * c000[k] + w1 * c1[k] + w2 * c2[k] + w3 * c111[k];
}

// Grain: value noise from an integer hash, so that it never repeats across the image.
inline uint32_t hash(uint32_t x, uint32_t y, uint32_t seed)
{
	uint32_t h = x * 0x8da6b343u ^ y * 0xd8163841u ^ seed * 0xcb1ab31fu;
	h ^= h >> 15;
	h *= 0x2c1b3c6du;
	h ^= h >> 12;
	h *= 0x297a2d39u;
	h ^= h >> 15;
	return h;
}

// Roughly gaussian, mean 0, standard deviation ~0.4.
inline float noise_at(uint32_t x, uint32_t y, uint32_t seed)
{
	uint32_t const h = hash(x, y, seed);
	return ((h & 0xffff) + (h >> 16)) * (1.0f / 65535.0f) - 1.0f;
}

struct Grain
{
	float strength[256]; // amplitude for each luma level, in levels
	float inv_size;
	uint32_t seed;
};

// Fills whole rows of grain at once: a tight loop, instead of a computation per pixel in
// the main loop. Grains bigger than a pixel are bilinear value noise on a coarser grid;
// each grid row is hashed once, kept while the rows below need it.
class GrainRows
{
public:
	GrainRows(Grain const &grain, unsigned width) : grain_(grain), width_(width), x0_(width), tx_(width)
	{
		unsigned const points = static_cast<unsigned>(width * grain.inv_size) + 2;
		for (auto &row : grid_)
			row.values.resize(points);
		for (unsigned x = 0; x < width; x++)
		{
			float const fx = x * grain.inv_size;
			x0_[x] = static_cast<uint32_t>(fx);
			tx_[x] = fx - x0_[x];
		}
	}

	void Fill(unsigned y, float *out)
	{
		if (grain_.inv_size >= 1)
		{
			for (unsigned x = 0; x < width_; x++)
				out[x] = noise_at(x, y, grain_.seed);
			return;
		}
		float const fy = y * grain_.inv_size;
		uint32_t const y0 = static_cast<uint32_t>(fy);
		float const ty = fy - y0;
		float const *r0 = GridRow(y0), *r1 = GridRow(y0 + 1);
		for (unsigned x = 0; x < width_; x++)
		{
			uint32_t const x0 = x0_[x];
			float const tx = tx_[x];
			float const top = r0[x0] + (r0[x0 + 1] - r0[x0]) * tx, bottom = r1[x0] + (r1[x0 + 1] - r1[x0]) * tx;
			// Interpolation smooths the noise: make up for the lost amplitude.
			out[x] = 1.6f * (top + (bottom - top) * ty);
		}
	}

private:
	struct CachedRow
	{
		uint32_t y = UINT32_MAX;
		std::vector<float> values;
	};

	float const *GridRow(uint32_t y)
	{
		CachedRow *top = &grid_[0];
		for (auto &row : grid_)
		{
			if (row.y == y)
				return row.values.data();
			if (row.y == UINT32_MAX || (top->y != UINT32_MAX && row.y < top->y))
				top = &row;
		}
		// Rows go downwards: the highest cached row is the one no longer needed.
		CachedRow &row = *top;
		row.y = y;
		for (unsigned x = 0; x < row.values.size(); x++)
			row.values[x] = noise_at(x, y, grain_.seed);
		return row.values.data();
	}

	Grain const &grain_;
	unsigned width_;
	std::vector<uint32_t> x0_;
	std::vector<float> tx_;
	std::array<CachedRow, 3> grid_;
};

void apply_rows(Look const &look, Grain const *grain, uint8_t *frame, unsigned width, unsigned height,
				unsigned stride, unsigned row0, unsigned row1)
{
	unsigned const uv_stride = stride / 2;
	uint8_t *const u_plane = frame + static_cast<size_t>(stride) * height;
	uint8_t *const v_plane = u_plane + static_cast<size_t>(uv_stride) * (height / 2);
	float const cx = width / 2.0f, cy = height / 2.0f, inv_r2 = 1.0f / (cx * cx + cy * cy);
	std::optional<GrainRows> grain_rows;
	std::vector<float> noise[2];
	if (grain)
	{
		grain_rows.emplace(*grain, width);
		noise[0].resize(width), noise[1].resize(width);
	}

	for (unsigned y = row0; y < row1; y += 2)
	{
		uint8_t *rows[2] = { frame + static_cast<size_t>(y) * stride, frame + static_cast<size_t>(y + 1) * stride };
		uint8_t *u = u_plane + static_cast<size_t>(y / 2) * uv_stride;
		uint8_t *v = v_plane + static_cast<size_t>(y / 2) * uv_stride;
		float const dy = y + 1 - cy;
		if (grain)
			grain_rows->Fill(y, noise[0].data()), grain_rows->Fill(y + 1, noise[1].data());
		for (unsigned bx = 0; bx < width / 2; bx++)
		{
			unsigned const x = 2 * bx;
			float cu = u[bx] - 128.0f, cv = v[bx] - 128.0f;
			float k = 1;
			if (look.vignette > 0)
			{
				float const dx = x + 1 - cx, r2 = (dx * dx + dy * dy) * inv_r2; // 1 in the corners
				k = 1 - look.vignette * r2 * r2;
				cu *= k, cv *= k;
			}
			float const dr = kRV * cv, dg = -kGU * cu - kGV * cv, db = kBU * cu;
			// The 4 pixels of the block, stage by stage rather than one after the other: the
			// Cortex-A53 runs in order, so independent computations side by side keep it busy.
			uint8_t *const px[4] = { &rows[0][x], &rows[0][x + 1], &rows[1][x], &rows[1][x + 1] };
			float r[4], g[4], b[4], out[4];
			for (int p = 0; p < 4; p++)
			{
				float const yy = *px[p] * k;
				r[p] = yy + dr, g[p] = yy + dg, b[p] = yy + db;
			}
			if (look.lut)
				for (int p = 0; p < 4; p++)
				{
					float rgb[3];
					lut_lookup(*look.lut, r[p], g[p], b[p], rgb);
					r[p] = rgb[0], g[p] = rgb[1], b[p] = rgb[2];
				}
			for (int p = 0; p < 4; p++)
				out[p] = 0.299f * r[p] + 0.587f * g[p] + 0.114f * b[p];
			if (grain)
				for (int p = 0; p < 4; p++)
					out[p] += grain->strength[to_u8(out[p])] * noise[p >> 1][x + (p & 1)];
			for (int p = 0; p < 4; p++)
				*px[p] = to_u8(out[p]);
			float const sr = 0.25f * (r[0] + r[1] + r[2] + r[3]), sg = 0.25f * (g[0] + g[1] + g[2] + g[3]),
						sb = 0.25f * (b[0] + b[1] + b[2] + b[3]);
			u[bx] = to_u8(128 - 0.168736f * sr - 0.331264f * sg + 0.5f * sb);
			v[bx] = to_u8(128 + 0.5f * sr - 0.418688f * sg - 0.081312f * sb);
		}
	}
}

} // namespace

std::shared_ptr<const Lut3D> Lut3D::Load(std::string const &path)
{
	std::ifstream file(path, std::ios::binary);
	if (!file)
		throw std::runtime_error("cannot open " + path);
	char magic[8];
	uint32_t header[2];
	file.read(magic, sizeof(magic));
	file.read(reinterpret_cast<char *>(header), sizeof(header));
	if (!file || std::memcmp(magic, kMagic, sizeof(kMagic)) || header[0] != kVersion)
		throw std::runtime_error(path + ": not a pycam LUT (version 1)");
	unsigned const n = header[1];
	if (n < 2 || n > 65)
		throw std::runtime_error(path + ": LUT size must be 2 to 65");
	std::vector<uint16_t> values(static_cast<size_t>(n) * n * n * 3);
	file.read(reinterpret_cast<char *>(values.data()), values.size() * sizeof(uint16_t));
	if (!file || file.peek() != std::char_traits<char>::eof())
		throw std::runtime_error(path + ": wrong LUT data size");
	auto lut = std::make_shared<Lut3D>();
	lut->size = n;
	lut->rgb.resize(values.size());
	for (size_t i = 0; i < values.size(); i++)
		lut->rgb[i] = values[i] * (255.0f / 65535.0f);
	return lut;
}

std::shared_ptr<const Lut3D> Lut3D::Identity(unsigned size)
{
	auto lut = std::make_shared<Lut3D>();
	lut->size = size;
	lut->rgb.resize(static_cast<size_t>(size) * size * size * 3);
	float const step = 255.0f / (size - 1);
	size_t i = 0;
	for (unsigned b = 0; b < size; b++)
		for (unsigned g = 0; g < size; g++)
			for (unsigned r = 0; r < size; r++)
			{
				lut->rgb[i++] = r * step;
				lut->rgb[i++] = g * step;
				lut->rgb[i++] = b * step;
			}
	return lut;
}

void apply_preview(Preview const &preview, uint8_t *frame, unsigned width, unsigned height, unsigned stride)
{
	// Fixed point, 8 fractional bits.
	int const mix_cb = static_cast<int>(std::lround(preview.mix_cb * 256)),
			  mix_cr = static_cast<int>(std::lround(preview.mix_cr * 256));
	int m[4];
	for (int i = 0; i < 4; i++)
		m[i] = static_cast<int>(std::lround(preview.chroma[i] * 256));
	bool const mix = mix_cb || mix_cr;
	unsigned const uv_stride = stride / 2;
	uint8_t *const u_plane = frame + static_cast<size_t>(stride) * height;
	uint8_t *const v_plane = u_plane + static_cast<size_t>(uv_stride) * (height / 2);
	uint8_t const *const luma = preview.luma.data();

	for (unsigned y = 0; y < height; y += 2)
	{
		uint8_t *rows[2] = { frame + static_cast<size_t>(y) * stride, frame + static_cast<size_t>(y + 1) * stride };
		uint8_t *u = u_plane + static_cast<size_t>(y / 2) * uv_stride;
		uint8_t *v = v_plane + static_cast<size_t>(y / 2) * uv_stride;
		for (unsigned bx = 0; bx < width / 2; bx++)
		{
			int const cb = u[bx] - 128, cr = v[bx] - 128;
			if (mix)
			{
				int const offset = (mix_cb * cb + mix_cr * cr) >> 8;
				for (uint8_t *row : rows)
				{
					row[2 * bx] = luma[std::clamp(row[2 * bx] + offset, 0, 255)];
					row[2 * bx + 1] = luma[std::clamp(row[2 * bx + 1] + offset, 0, 255)];
				}
			}
			else
				for (uint8_t *row : rows)
					row[2 * bx] = luma[row[2 * bx]], row[2 * bx + 1] = luma[row[2 * bx + 1]];
			u[bx] = static_cast<uint8_t>(std::clamp(128 + ((m[0] * cb + m[1] * cr) >> 8), 0, 255));
			v[bx] = static_cast<uint8_t>(std::clamp(128 + ((m[2] * cb + m[3] * cr) >> 8), 0, 255));
		}
	}
}

void apply_look(Look const &look, uint8_t *frame, unsigned width, unsigned height, unsigned stride, uint64_t seed,
				unsigned threads)
{
	if (look.Empty())
		return;
	std::unique_ptr<Grain> grain;
	if (look.grain > 0)
	{
		grain = std::make_unique<Grain>();
		grain->inv_size = 1.0f / std::max(look.grain_size, 0.5f);
		grain->seed = static_cast<uint32_t>(seed * 2654435761u);
		// Like film grain, strongest in the mid-tones, still visible in the shadows.
		for (int level = 0; level < 256; level++)
		{
			float const t = level / 255.0f;
			grain->strength[level] = look.grain * 24.0f * (0.3f + 0.7f * 4 * t * (1 - t));
		}
	}
	threads = std::max(1u, threads);
	std::vector<std::thread> pool;
	for (unsigned i = 0; i < threads; i++)
	{
		unsigned const row0 = (height * i / threads) & ~1u, row1 = (height * (i + 1) / threads) & ~1u;
		pool.emplace_back(apply_rows, std::cref(look), grain.get(), frame, width, height, stride, row0, row1);
	}
	for (auto &thread : pool)
		thread.join();
}

} // namespace pycam
