#pragma once

#include <string>
#include <vector>

#include <libcamera/base/span.h>
#include <libcamera/controls.h>

#include "core/stream_info.hpp"

namespace pycam
{

struct DngOptions
{
	// Lossless JPEG (LJ92), as in CinemaDNG: about half the size, for some CPU time.
	bool compress = false;
	// The tags of the capture command ("EXIF.FocalLength=1600/100"…). Those about the
	// lens and the aperture also go into the DNG (EXIF IFD, and LensInfo).
	std::vector<std::string> exif;
};

// Writes a raw frame as a DNG. Based on the rpicam-apps DNG writer (image/dng.cpp,
// BSD-2-Clause, Raspberry Pi Ltd), which takes no extra tags and does not compress.
void dng_write(libcamera::Span<const uint8_t> raw, StreamInfo const &info, libcamera::ControlList const &metadata,
			   std::string const &filename, std::string const &camera_model, DngOptions const &options);

// The same from samples already unpacked, one uint16 per pixel (`info.width` per row),
// in the raw format's bit depth: a sum of exposures (stack.hpp). `pixels` may be reused.
void dng_write_unpacked(std::vector<uint16_t> &pixels, StreamInfo const &info, libcamera::ControlList const &metadata,
						std::string const &filename, std::string const &camera_model, DngOptions const &options);

// A raw frame (packed 10 or 12 bits, or 16 bits) into one uint16 per pixel, `info.width`
// per row. Throws for formats it does not know.
void unpack_raw(uint8_t const *raw, StreamInfo const &info, uint16_t *dest);
int raw_bits(StreamInfo const &info);
// Bayer order of the raw format: 0 RGGB, 1 GRBG, 2 GBRG, 3 BGGR; -1 for a monochrome sensor.
int raw_bayer_order(StreamInfo const &info);

} // namespace pycam
