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

} // namespace pycam
