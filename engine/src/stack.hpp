#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "core/stream_info.hpp"

namespace pycam
{

// Raw frames combined into one (docs/architecture.md §2.4): the bulb exposure and, later,
// stacking. The sensor fixes each exposure before it starts, so an exposure that ends
// when the shutter is released is made of short ones, added up.
//
// - add: their sum, clipped where the sensor would saturate: one exposure as long as all
//   of them (the read noise grows as the square root of their number);
// - mean: their average, as one of them but less noisy;
// - max: the brightest of each pixel (light trails, star trails).
class RawStack
{
public:
	enum class Mode
	{
		Add,
		Mean,
		Max,
	};

	static Mode ParseMode(std::string const &name); // "add", "mean", "max"; throws std::invalid_argument

	explicit RawStack(Mode mode) : mode_(mode) {}

	// One more frame, packed as the camera gives it. black: the black level, in raw units.
	void Add(uint8_t const *raw, StreamInfo const &info, unsigned black);
	// The combined frame, one uint16 per pixel (`info.width` per row), black level kept
	// and clipped to `white`: what a single frame would hold. Frees the sums.
	std::vector<uint16_t> Take(unsigned white);

	Mode mode() const { return mode_; }
	unsigned frames() const { return frames_; }
	unsigned width() const { return width_; }
	unsigned height() const { return height_; }

private:
	Mode mode_;
	unsigned frames_ = 0;
	unsigned width_ = 0, height_ = 0;
	unsigned black_ = 0;
	std::vector<uint32_t> sum_; // add, mean: sums; max: maxima
};

} // namespace pycam
