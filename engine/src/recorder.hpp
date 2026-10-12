#pragma once

#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>

#include <libcamera/stream.h>
#include <nlohmann/json.hpp>

#include "core/completed_request.hpp"
#include "core/rpicam_app.hpp"
#include "core/stream_info.hpp"
#include "core/video_options.hpp"
#include "encoder/encoder.hpp"

struct AVFormatContext;
struct AVStream;

namespace pycam
{

// Records the video stream into an MP4 (docs/architecture.md §2.5): the VideoCore's H.264
// encoder (rpicam-apps' H264Encoder) reads the camera buffers themselves (dmabuf, no
// copy), and libavformat puts what it gives into the file, no sound (the Pi has no
// microphone). The file is written as .part, then renamed once complete.
//
// Each camera frame stays held until the encoder is done with it. Stop() before the camera
// stops: the frames it holds go back to the camera first (see Engine::ReleaseFrames).
class Recorder
{
public:
	using EventFn = std::function<void(nlohmann::json)>;

	// path: the final file (".MP4"). slowdown: the file plays that many times slower than
	// it was filmed (2: 50 frames a second played at 25, a slow motion). Throws
	// std::runtime_error if it cannot start.
	Recorder(std::string path, StreamInfo const &info, unsigned fps, unsigned bitrate, EventFn on_event,
			 unsigned slowdown = 1);
	~Recorder();

	// A camera frame of the video stream (camera loop thread).
	void Encode(CompletedRequestPtr const &request, libcamera::Stream *stream, RPiCamApp &app);
	// Ends the file: {"path", "duration" (filmed), "video_duration" (played), "frames",
	// "bytes"}, or "error".
	nlohmann::json Stop();
	nlohmann::json State() const;

private:
	void OnInputDone(void *mem);
	void OnOutput(void *mem, size_t size, int64_t timestamp_us, bool keyframe);
	void StartFile(uint8_t const *data, size_t size);

	std::string path_, part_;
	StreamInfo info_;
	unsigned fps_;
	unsigned slowdown_;
	EventFn on_event_;
	VideoOptions options_;
	std::unique_ptr<Encoder> encoder_;

	mutable std::mutex mutex_; // encoding_, and the counters read by State()
	std::deque<CompletedRequestPtr> encoding_; // frames the encoder still reads, oldest first

	// Encoder output thread, then Stop().
	AVFormatContext *format_ = nullptr;
	AVStream *stream_ = nullptr;
	bool started_ = false;
	int64_t first_us_ = -1, last_us_ = 0, last_progress_us_ = 0;
	uint64_t frames_ = 0, bytes_ = 0;
	double duration_ = 0; // so far, in seconds
	std::string error_;
	bool stopped_ = false;
};

} // namespace pycam
