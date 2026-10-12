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
	float strength[256]; // luma grain amplitude for each luma level, in levels
	float inv_size; // 1 / the grain size, in pixels of this image
	float colour; // grain in the chroma, against the luma grain (colour films: three dye layers)
	float clumps; // share of the coarser clumps
	uint32_t seed;
};

// Fills whole rows of grain at once: a tight loop, instead of a computation per pixel in
// the main loop. Grains bigger than a pixel are bilinear value noise on a coarser grid;
// each grid row is hashed once, kept while the rows below need it.
class GrainRows
{
public:
	GrainRows(float inv_size, uint32_t seed, unsigned width)
		: inv_size_(inv_size), seed_(seed), width_(width), x0_(width), tx_(width)
	{
		unsigned const points = static_cast<unsigned>(width * inv_size) + 2;
		for (auto &row : grid_)
			row.values.resize(points);
		for (unsigned x = 0; x < width; x++)
		{
			float const fx = x * inv_size;
			x0_[x] = static_cast<uint32_t>(fx);
			tx_[x] = fx - x0_[x];
		}
	}

	void Fill(unsigned y, float *out)
	{
		if (inv_size_ >= 1)
		{
			for (unsigned x = 0; x < width_; x++)
				out[x] = noise_at(x, y, seed_);
			return;
		}
		float const fy = y * inv_size_;
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
			row.values[x] = noise_at(x, y, seed_);
		return row.values.data();
	}

	float inv_size_;
	uint32_t seed_;
	unsigned width_;
	std::vector<uint32_t> x0_;
	std::vector<float> tx_;
	std::array<CachedRow, 3> grid_;
};

// Halation: where the light spreads, on a coarse grid (cells of a few pixels), blurred.
class HaloMap
{
public:
	HaloMap(Look const &look, uint8_t const *frame, unsigned width, unsigned height, unsigned stride, float scale)
	{
		float const sigma = std::max(1.0f, look.halation_radius * scale);
		cell_ = std::clamp(static_cast<unsigned>(sigma / 3), 2u, 16u); // ~3 cells per sigma
		w_ = (width + cell_ - 1) / cell_, h_ = (height + cell_ - 1) / cell_;
		values_.assign(static_cast<size_t>(w_) * h_, 0.0f);
		// The bright parts, in the red: the light that reaches the film base and comes back
		// to the red layer is mostly red, the other layers (and the yellow filter of colour
		// films) having taken the rest. How far above the threshold, averaged over each cell.
		float const t = std::clamp(look.halation_threshold, 0.0f, 0.99f);
		float light[256];
		for (int level = 0; level < 256; level++)
		{
			float const above = std::max(0.0f, (level / 255.0f - t) / (1 - t));
			light[level] = above * above; // a soft start, then fast: the brightest lights glow
		}
		uint8_t const *const v_plane = frame + static_cast<size_t>(stride) * height + static_cast<size_t>(stride / 2) * (height / 2);
		unsigned const step = cell_ >= 8 ? 4 : 2;
		for (unsigned cy = 0; cy < h_; cy++)
			for (unsigned cx = 0; cx < w_; cx++)
			{
				float sum = 0;
				unsigned n = 0;
				// One pixel in `step` each way is plenty for a glow tens of pixels wide.
				for (unsigned y = cy * cell_; y < std::min(height, (cy + 1) * cell_); y += step)
				{
					uint8_t const *luma = frame + static_cast<size_t>(y) * stride;
					uint8_t const *v = v_plane + static_cast<size_t>(y / 2) * (stride / 2);
					for (unsigned x = cx * cell_; x < std::min(width, (cx + 1) * cell_); x += step)
					{
						int const red = luma[x] + ((359 * (v[x / 2] - 128)) >> 8); // R = Y + 1.402 Cr
						sum += light[std::clamp(red, 0, 255)], n++;
					}
				}
				values_[static_cast<size_t>(cy) * w_ + cx] = n ? sum / n : 0;
			}
		// Three box blurs in each direction: close to a gaussian of this sigma.
		float const sigma_cells = sigma / cell_;
		int const radius = std::max(1, static_cast<int>(std::lround((std::sqrt(4 * sigma_cells * sigma_cells + 1) - 1) / 2)));
		std::vector<float> tmp(values_.size());
		for (int pass = 0; pass < 3; pass++)
		{
			BoxBlur(values_.data(), tmp.data(), w_, h_, 1, w_, radius);
			BoxBlur(tmp.data(), values_.data(), h_, w_, w_, 1, radius);
		}
		// Where the centre of each 2x2 block falls between the cells, along x.
		unsigned const blocks = width / 2;
		x0_.resize(blocks), tx_.resize(blocks);
		for (unsigned bx = 0; bx < blocks; bx++)
		{
			float const fx = std::clamp((2 * bx + 1.0f) / cell_ - 0.5f, 0.0f, w_ - 1.0f);
			x0_[bx] = std::min(static_cast<unsigned>(fx), w_ > 1 ? w_ - 2 : 0);
			tx_[bx] = fx - x0_[bx];
		}
	}

	// The map at the centre of each 2x2 block of a row of blocks (pixel row y), bilinear
	// between the cell centres: one row at a time, the positions along x computed once.
	void FillRow(float y, float *out) const
	{
		unsigned const blocks = x0_.size();
		float const fy = std::clamp(y / cell_ - 0.5f, 0.0f, h_ - 1.0f);
		unsigned const y0 = std::min(static_cast<unsigned>(fy), h_ > 1 ? h_ - 2 : 0), y1 = std::min(y0 + 1, h_ - 1);
		float const ty = fy - y0;
		float const *r0 = &values_[static_cast<size_t>(y0) * w_], *r1 = &values_[static_cast<size_t>(y1) * w_];
		unsigned const last = w_ - 1;
		for (unsigned bx = 0; bx < blocks; bx++)
		{
			unsigned const x0 = x0_[bx], x1 = std::min(x0 + 1, last);
			float const tx = tx_[bx];
			float const top = r0[x0] + (r0[x1] - r0[x0]) * tx, bottom = r1[x0] + (r1[x1] - r1[x0]) * tx;
			out[bx] = top + (bottom - top) * ty;
		}
	}

private:
	// Along lines of `length` values `step` apart, `lines` of them `line_step` apart; the
	// edges are repeated.
	static void BoxBlur(float const *in, float *out, unsigned length, unsigned lines, unsigned step,
						unsigned line_step, int radius)
	{
		float const norm = 1.0f / (2 * radius + 1);
		for (unsigned l = 0; l < lines; l++)
		{
			float const *src = in + static_cast<size_t>(l) * line_step;
			float *dst = out + static_cast<size_t>(l) * line_step;
			auto at = [&](int i) { return src[static_cast<size_t>(std::clamp(i, 0, int(length) - 1)) * step]; };
			float sum = 0;
			for (int i = -radius; i <= radius; i++)
				sum += at(i);
			for (unsigned i = 0; i < length; i++)
			{
				dst[static_cast<size_t>(i) * step] = sum * norm;
				sum += at(int(i) + radius + 1) - at(int(i) - radius);
			}
		}
	}

	unsigned cell_, w_, h_;
	std::vector<float> values_;
	std::vector<unsigned> x0_;
	std::vector<float> tx_;
};

void apply_rows(Look const &look, Grain const *grain, HaloMap const *halo, uint8_t *frame, unsigned width,
				unsigned height, unsigned stride, unsigned row0, unsigned row1)
{
	unsigned const uv_stride = stride / 2;
	uint8_t *const u_plane = frame + static_cast<size_t>(stride) * height;
	uint8_t *const v_plane = u_plane + static_cast<size_t>(uv_stride) * (height / 2);
	float const cx = width / 2.0f, cy = height / 2.0f, inv_r2 = 1.0f / (cx * cx + cy * cy);
	// Grain: fine noise, maybe mixed with coarser clumps; for colour films, two more noises
	// on the chroma, per 2x2 block (there, the grain is half as many blocks).
	std::vector<float> halo_row(halo ? width / 2 : 0);
	std::optional<GrainRows> fine, coarse, chroma[2];
	std::vector<float> noise[2], clumps, chroma_noise[2];
	float fine_share = 1, clump_share = 0;
	if (grain)
	{
		fine.emplace(grain->inv_size, grain->seed, width);
		noise[0].resize(width), noise[1].resize(width);
		if (grain->clumps > 0)
		{
			coarse.emplace(grain->inv_size / 2.5f, grain->seed ^ 0x9e3779b9u, width);
			clumps.resize(width);
			// Two independent noises mixed: keep the overall amplitude.
			float const norm = 1 / std::hypot(1 - grain->clumps, grain->clumps);
			fine_share = (1 - grain->clumps) * norm, clump_share = grain->clumps * norm;
		}
		if (grain->colour > 0)
			for (int c = 0; c < 2; c++)
			{
				chroma[c].emplace(grain->inv_size * 2, grain->seed ^ (c ? 0xc2b2ae35u : 0x85ebca6bu), width / 2);
				chroma_noise[c].resize(width / 2);
			}
	}

	for (unsigned y = row0; y < row1; y += 2)
	{
		uint8_t *rows[2] = { frame + static_cast<size_t>(y) * stride, frame + static_cast<size_t>(y + 1) * stride };
		uint8_t *u = u_plane + static_cast<size_t>(y / 2) * uv_stride;
		uint8_t *v = v_plane + static_cast<size_t>(y / 2) * uv_stride;
		float const dy = y + 1 - cy;
		if (grain)
		{
			for (unsigned j = 0; j < 2; j++)
			{
				fine->Fill(y + j, noise[j].data());
				if (coarse)
				{
					coarse->Fill(y + j, clumps.data());
					for (unsigned x = 0; x < width; x++)
						noise[j][x] = fine_share * noise[j][x] + clump_share * clumps[x];
				}
			}
			if (grain->colour > 0)
				chroma[0]->Fill(y / 2, chroma_noise[0].data()), chroma[1]->Fill(y / 2, chroma_noise[1].data());
		}
		if (halo)
			halo->FillRow(y + 1.0f, halo_row.data());
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
			float dr = kRV * cv, dg = -kGU * cu - kGV * cv, db = kBU * cu;
			if (halo && halo_row[bx] > 1e-4f)
			{
				// Saturating: a small light glows a little, a large bright area fringes.
				float const glow = look.halation * 160 * (1 - std::exp(-4 * halo_row[bx]));
				dr += glow * look.halation_colour[0], dg += glow * look.halation_colour[1],
					db += glow * look.halation_colour[2];
			}
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
			float uu = 128 - 0.168736f * sr - 0.331264f * sg + 0.5f * sb;
			float vv = 128 + 0.5f * sr - 0.418688f * sg - 0.081312f * sb;
			if (grain && grain->colour > 0)
			{
				float const a = grain->colour * 0.5f * grain->strength[to_u8(0.25f * (out[0] + out[1] + out[2] + out[3]))];
				uu += a * chroma_noise[0][bx], vv += a * chroma_noise[1][bx];
			}
			u[bx] = to_u8(uu);
			v[bx] = to_u8(vv);
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
		// Sizes are given for the full sensor: a smaller JPEG (sizes M, S) has smaller grains,
		// and a little less of them, averaged by the downscaling.
		float const scale = std::clamp(width / 4056.0f, 0.25f, 1.0f);
		grain->inv_size = 1.0f / std::max(look.grain_size * scale, 0.5f);
		grain->colour = look.grain_colour;
		grain->clumps = look.grain_clumps;
		grain->seed = static_cast<uint32_t>(seed * 2654435761u);
		// Like film grain: strongest in the mid-tones (slides), or lower down, in the shadows
		// (negatives: the thin parts of the negative); still a little in the blacks.
		float const amplitude = look.grain * 24.0f * std::sqrt(scale);
		for (int level = 0; level < 256; level++)
		{
			float const t = level / 255.0f;
			float const mids = 4 * t * (1 - t), shadows = 6.75f * t * (1 - t) * (1 - t);
			grain->strength[level] =
				amplitude * (0.3f + 0.7f * ((1 - look.grain_shadows) * mids + look.grain_shadows * shadows));
		}
	}
	std::unique_ptr<HaloMap> halo;
	if (look.halation > 0)
		halo = std::make_unique<HaloMap>(look, frame, width, height, stride, std::clamp(width / 4056.0f, 0.25f, 1.0f));
	threads = std::max(1u, threads);
	std::vector<std::thread> pool;
	for (unsigned i = 0; i < threads; i++)
	{
		unsigned const row0 = (height * i / threads) & ~1u, row1 = (height * (i + 1) / threads) & ~1u;
		pool.emplace_back(apply_rows, std::cref(look), grain.get(), halo.get(), frame, width, height, stride, row0, row1);
	}
	for (auto &thread : pool)
		thread.join();
}

} // namespace pycam
