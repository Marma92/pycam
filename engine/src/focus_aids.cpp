#include "focus_aids.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <map>
#include <stdexcept>

using nlohmann::json;

namespace pycam
{

namespace
{

struct Sensitivity
{
	double fraction; // share of the blocks that may light up
	unsigned floor; // minimal gradient, in luma levels: a blurry image stays clean
};

const std::map<std::string, Sensitivity> kSensitivities = {
	{ "low", { 0.015, 30 } },
	{ "medium", { 0.03, 20 } },
	{ "high", { 0.06, 12 } },
};

const std::map<std::string, std::array<uint8_t, 3>> kColors = {
	{ "red", { 255, 0, 0 } },	  { "green", { 0, 255, 0 } },	  { "blue", { 40, 90, 255 } },
	{ "yellow", { 255, 230, 0 } }, { "white", { 255, 255, 255 } },
};

// Full range BT.601: the colour space of the viewfinder stream (sYCC).
std::array<uint8_t, 3> rgb_to_yuv(std::array<uint8_t, 3> const &rgb)
{
	double r = rgb[0], g = rgb[1], b = rgb[2];
	auto byte = [](double v) { return static_cast<uint8_t>(std::clamp(std::lround(v), 0L, 255L)); };
	return { byte(0.299 * r + 0.587 * g + 0.114 * b), byte(128 - 0.168736 * r - 0.331264 * g + 0.5 * b),
			 byte(128 + 0.5 * r - 0.418688 * g - 0.081312 * b) };
}

} // namespace

void FocusAids::Configure(json const &params)
{
	std::lock_guard<std::mutex> lock(mutex_);
	Settings s = settings_;
	if (params.contains("peaking"))
		s.peaking = params["peaking"].get<bool>();
	if (params.contains("peaking_color"))
	{
		json const &color = params["peaking_color"];
		std::array<uint8_t, 3> rgb;
		if (color.is_string())
		{
			auto it = kColors.find(color.get<std::string>());
			if (it == kColors.end())
				throw std::invalid_argument("peaking_color: red, green, blue, yellow, white or [r, g, b]");
			rgb = it->second;
			s.color = it->first;
		}
		else if (color.is_array() && color.size() == 3)
		{
			for (int i = 0; i < 3; i++)
				rgb[i] = static_cast<uint8_t>(std::clamp(color[i].get<int>(), 0, 255));
			s.color = "custom";
		}
		else
			throw std::invalid_argument("peaking_color: red, green, blue, yellow, white or [r, g, b]");
		s.yuv = rgb_to_yuv(rgb);
	}
	if (params.contains("peaking_sensitivity"))
	{
		std::string sensitivity = params["peaking_sensitivity"].get<std::string>();
		if (!kSensitivities.count(sensitivity))
			throw std::invalid_argument("peaking_sensitivity: low, medium or high");
		s.sensitivity = sensitivity;
	}
	if (params.contains("zebra"))
		s.zebra = params["zebra"].get<bool>();
	if (params.contains("zebra_level"))
	{
		float level = params["zebra_level"].get<float>();
		if (!(level > 0 && level <= 1))
			throw std::invalid_argument("zebra_level: between 0 and 1");
		s.zebra_level = level;
	}
	settings_ = s;
}

json FocusAids::State() const
{
	std::lock_guard<std::mutex> lock(mutex_);
	return {
		{ "peaking", settings_.peaking },
		{ "peaking_color", settings_.color },
		{ "peaking_sensitivity", settings_.sensitivity },
		{ "zebra", settings_.zebra },
		{ "zebra_level", settings_.zebra_level },
		{ "last_ms", last_ms_.load() },
	};
}

bool FocusAids::Active() const
{
	std::lock_guard<std::mutex> lock(mutex_);
	return settings_.peaking || settings_.zebra;
}

void FocusAids::Apply(uint8_t *frame, StreamInfo const &info)
{
	Settings s;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		s = settings_;
	}
	if (!s.peaking && !s.zebra)
		return;
	auto start = std::chrono::steady_clock::now();

	// YUV420: a 2x2 block of Y pixels shares one U and one V sample.
	unsigned const stride = info.stride, uv_stride = stride / 2;
	unsigned const bw = info.width / 2, bh = info.height / 2;
	if (bw < 3 || bh < 3)
		return;
	uint8_t *const y_plane = frame;
	uint8_t *const u_plane = frame + size_t(stride) * info.height;
	uint8_t *const v_plane = u_plane + size_t(uv_stride) * (info.height / 2);

	// 1. Sum of the four pixels of each block (0..1020): averaging tames the noise.
	blocks_.resize(size_t(bw) * bh);
	for (unsigned by = 0; by < bh; by++)
	{
		uint8_t const *row0 = y_plane + size_t(2 * by) * stride;
		uint8_t const *row1 = row0 + stride;
		uint16_t *out = &blocks_[size_t(by) * bw];
		for (unsigned bx = 0; bx < bw; bx++)
			out[bx] = row0[2 * bx] + row0[2 * bx + 1] + row1[2 * bx] + row1[2 * bx + 1];
	}

	// 2. Gradient of each block against its neighbours, histogram for the next threshold,
	//    and painting: peaking (luma and chroma of the block), else zebras (luma stripes).
	unsigned const threshold = threshold_;
	unsigned const zebra_sum = static_cast<unsigned>(s.zebra_level * 255 * 4);
	unsigned const phase = 2 * frame_count_++; // the stripes crawl, like on a camcorder
	histogram_.fill(0);
	for (unsigned by = 1; by + 1 < bh; by++)
	{
		uint16_t const *up = &blocks_[size_t(by - 1) * bw];
		uint16_t const *mid = up + bw;
		uint16_t const *down = mid + bw;
		uint8_t *row[2] = { y_plane + size_t(2 * by) * stride, y_plane + size_t(2 * by + 1) * stride };
		uint8_t *u = u_plane + size_t(by) * uv_stride;
		uint8_t *v = v_plane + size_t(by) * uv_stride;
		for (unsigned bx = 1; bx + 1 < bw; bx++)
		{
			// Central differences, in luma levels (0..510).
			unsigned g = (std::abs(mid[bx + 1] - mid[bx - 1]) + std::abs(down[bx] - up[bx])) / 4;
			histogram_[std::min(g, 255u)]++;
			if (s.peaking && g > threshold)
			{
				row[0][2 * bx] = row[0][2 * bx + 1] = row[1][2 * bx] = row[1][2 * bx + 1] = s.yuv[0];
				u[bx] = s.yuv[1];
				v[bx] = s.yuv[2];
			}
			else if (s.zebra && mid[bx] >= zebra_sum)
			{
				for (unsigned dy = 0; dy < 2; dy++)
					for (unsigned dx = 0; dx < 2; dx++)
						if (((2 * bx + dx + 2 * by + dy + phase) / 4) % 2 == 0)
							row[dy][2 * bx + dx] = 40;
			}
		}
	}

	// 3. Next frame's threshold: keep the sharpest share of the blocks, never below the floor.
	if (s.peaking)
	{
		Sensitivity const &sensitivity = kSensitivities.at(s.sensitivity);
		uint64_t const keep = static_cast<uint64_t>(double(bw - 2) * (bh - 2) * sensitivity.fraction);
		uint64_t count = 0;
		unsigned t = 255;
		while (t > 0 && count + histogram_[t] <= keep)
			count += histogram_[t--];
		threshold_ = std::max(t, sensitivity.floor);
	}

	last_ms_ = std::chrono::duration<float, std::milli>(std::chrono::steady_clock::now() - start).count();
}

} // namespace pycam
