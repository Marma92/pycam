#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "core/stream_info.hpp"

namespace pycam
{

// Focus peaking and zebras, burnt into the viewfinder frames just before they are shown
// (docs/architecture.md §2.3). The viewfinder buffers only feed the screen, so the photos
// are untouched; and drawing into the frame needs no extra display plane (the VC4 has
// little line buffer memory for scaled planes) and cannot lag behind the image.
//
// Peaking works on 2x2 blocks, the chroma resolution of YUV420: averaging four pixels
// tames the sensor noise, and a block is painted with its luma and chroma at once.
// Its threshold adapts to the scene: it keeps the sharpest few percent of the edges, but
// never goes below a floor, so that a blurry image stays clean.
class FocusAids
{
public:
	// Commands: {"peaking": bool, "peaking_color": "red"|…|[r, g, b],
	// "peaking_sensitivity": "low"|"medium"|"high", "zebra": bool, "zebra_level": 0..1}.
	// Missing keys keep their value. Throws std::invalid_argument.
	void Configure(nlohmann::json const &params);
	nlohmann::json State() const;
	bool Active() const;

	// Draws into a YUV420 frame mapped for the CPU (cache synced by the caller).
	void Apply(uint8_t *frame, StreamInfo const &info);

private:
	struct Settings
	{
		bool peaking = false;
		std::array<uint8_t, 3> yuv { 76, 85, 255 }; // red
		std::string color = "red";
		std::string sensitivity = "medium";
		bool zebra = false;
		float zebra_level = 0.95f;
	};

	mutable std::mutex mutex_;
	Settings settings_;

	// Display thread only.
	std::vector<uint16_t> blocks_; // sum of the 4 pixels of each 2x2 block
	std::array<uint32_t, 256> histogram_ {};
	unsigned threshold_ = 255; // from the previous frame
	uint32_t frame_count_ = 0;
	std::atomic<float> last_ms_ { 0 };
};

} // namespace pycam
