#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include <libcamera/controls.h>

#include "core/stream_info.hpp"

namespace pycam
{

// The ISP's rendering, done on the CPU for a raw frame the ISP never saw: the sum of a
// stack (stack.hpp). Same steps as the ISP of the Pi, with its settings for the frame
// (white balance, colour matrix: the frame's metadata) and the tuning's tone curve, so
// that the result sits on the same "pycam base" as the other photos (docs/presets.md):
// hot pixels, demosaicing (Malvar-He-Cutler), gains, colour matrix, tone curve, full
// range BT.601 YUV 4:2:0. No denoising, no sharpening, no lens shading correction.
struct DevelopParams
{
	int bayer_order = 0; // 0 RGGB, 1 GRBG, 2 GBRG, 3 BGGR
	float black = 256, white = 4095; // raw units
	float gain_r = 1, gain_b = 1, digital_gain = 1;
	std::array<float, 9> ccm { 1, 0, 0, 0, 1, 0, 0, 0, 1 }; // row major, on camera RGB
	std::vector<uint8_t> tone; // linear 0..1 (tone.size() steps) -> 8 bits; see tone_curve()
	bool hot_pixels = true;
};

// The tone curve of an ISP tuning file ("rpi.contrast", "gamma_curve"), sampled on `size`
// steps of linear light. Without one (no file, no curve): the Rec.709 curve.
std::vector<uint8_t> tone_curve(std::string const &tuning_file, size_t size = 16384);

// The parameters of a frame from its metadata (gains, colour matrix, black level).
DevelopParams develop_params(libcamera::ControlList const &metadata, StreamInfo const &raw_info);

// Develops `raw` (one uint16 per pixel, `width` per row; its hot pixels are fixed in place)
// into the YUV 4:2:0 image described by `yuv_info`, scaled down if it is smaller.
void develop(std::vector<uint16_t> &raw, unsigned width, unsigned height, DevelopParams const &params, uint8_t *yuv,
			 StreamInfo const &yuv_info, unsigned threads);

} // namespace pycam
