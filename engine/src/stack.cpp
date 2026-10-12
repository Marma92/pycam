#include "stack.hpp"

#include <algorithm>
#include <stdexcept>

#include "dng_writer.hpp"

namespace pycam
{

RawStack::Mode RawStack::ParseMode(std::string const &name)
{
	if (name == "add")
		return Mode::Add;
	if (name == "mean")
		return Mode::Mean;
	if (name == "max")
		return Mode::Max;
	throw std::invalid_argument("stack mode: add, mean or max");
}

void RawStack::Add(uint8_t const *raw, StreamInfo const &info, unsigned black)
{
	if (frames_ == 0)
	{
		width_ = info.width;
		height_ = info.height;
		black_ = black;
		sum_.assign(static_cast<size_t>(width_) * height_, 0);
	}
	else if (info.width != width_ || info.height != height_)
		throw std::runtime_error("stack: the frames changed size");

	// Row by row: a whole unpacked frame would cost another 24 MB.
	StreamInfo row_info = info;
	row_info.height = 1;
	std::vector<uint16_t> row(width_);
	for (unsigned y = 0; y < height_; y++)
	{
		unpack_raw(raw + static_cast<size_t>(y) * info.stride, row_info, row.data());
		uint32_t *sum = sum_.data() + static_cast<size_t>(y) * width_;
		if (mode_ == Mode::Max)
			for (unsigned x = 0; x < width_; x++)
				sum[x] = std::max<uint32_t>(sum[x], row[x]);
		else
			for (unsigned x = 0; x < width_; x++)
				sum[x] += row[x];
	}
	frames_++;
}

std::vector<uint16_t> RawStack::Take(unsigned white)
{
	if (frames_ == 0)
		throw std::runtime_error("stack: no frame");
	std::vector<uint16_t> out(sum_.size());
	uint32_t const n = frames_;
	for (size_t i = 0; i < sum_.size(); i++)
	{
		uint32_t v = sum_[i];
		if (mode_ == Mode::Add)
		{
			// The black level is in each frame: once in the result. The noise around it
			// goes below black in some frames: summed before clipping, it averages out.
			uint64_t const blacks = static_cast<uint64_t>(n - 1) * black_;
			v = v > blacks ? static_cast<uint32_t>(v - blacks) : 0;
		}
		else if (mode_ == Mode::Mean)
			v = (v + n / 2) / n;
		out[i] = static_cast<uint16_t>(std::min<uint32_t>(v, white));
	}
	std::vector<uint32_t>().swap(sum_);
	return out;
}

} // namespace pycam
