#pragma once

#include <condition_variable>
#include <cstdint>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include <nlohmann/json.hpp>

#include "display.hpp"

namespace pycam
{

// Plays a video (a timelapse) on the screen, instead of the viewfinder
// (docs/architecture.md §4): libavformat reads the file, the VideoCore's H.264 decoder
// (h264_v4l2m2m) decodes it into DRM PRIME buffers, and the display puts them on the video
// plane as they are, scaled by the hardware into the image area: no copy, 25 frames a
// second with nearly no CPU. The camera goes on running; the HUD stays above. Events:
// "video_progress" (about twice a second) and "video_ended"; the last frame stays until
// Stop().
//
// Everything libav does happens on the player's thread: the decoder is not made to be
// called from two threads, and giving its buffers back from the display thread stalled it
// (seen 2026-10-12). The display hands the frames it is done with to a queue that this
// thread empties.
class VideoPlayer
{
public:
	using EventFn = std::function<void(nlohmann::json)>;

	VideoPlayer(Display &display, EventFn on_event);
	~VideoPlayer(); // stops

	// Opens and starts the video (the one playing stops first). Returns its duration, size
	// and frame rate; throws std::runtime_error if it cannot be played.
	nlohmann::json Play(std::string const &path);
	void Pause(bool paused);
	void Stop(); // and back to the camera; never waits more than a few seconds
	nlohmann::json State() const;

	struct FreeQueue; // frames the display no longer shows, freed on the player's thread

private:
	struct Decoder;
	void Run(Decoder *decoder, uint64_t generation, std::promise<void> done);

	Display &display_;
	EventFn on_event_;
	std::shared_ptr<FreeQueue> free_;
	std::thread thread_;
	std::future<void> finished_;
	mutable std::mutex mutex_;
	std::condition_variable cond_;
	uint64_t generation_ = 0; // a stuck thread left behind must not show its frames
	bool stop_ = false;
	bool paused_ = false;
	bool active_ = false;
	bool stuck_ = false; // a decoder thread never came back: no more videos until a restart
	std::string path_;
	double position_ = 0, duration_ = 0;
};

} // namespace pycam
