#pragma once

#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <libcamera/controls.h>
#include <nlohmann/json.hpp>

#include "core/still_options.hpp"
#include "core/stream_info.hpp"

#include "look.hpp"

namespace pycam
{

// A captured frame, copied out of the camera buffers so that the camera can be
// reconfigured (and its buffers freed) while the files are being written.
struct CaptureJob
{
	uint64_t id = 0;
	std::string base_path; // without extension: ".JPG" / ".DNG" are appended
	bool jpeg = true;
	bool dng = true;
	bool compress_dng = false; // lossless JPEG (LJ92)
	std::vector<uint8_t> yuv;
	StreamInfo yuv_info;
	std::vector<uint8_t> raw;
	StreamInfo raw_info;
	libcamera::ControlList metadata;
	std::unique_ptr<StillOptions> options; // snapshot: JPEG quality, EXIF tags, thumbnail
	std::string camera_model;
	std::shared_ptr<const Look> look; // applied to the JPEG only
	std::string base_jpeg_path; // without extension; not empty: also write the JPEG without the look
};

// Writes capture jobs on a background thread, one at a time, and reports each one
// through `on_event` ("capture_done" or "capture_error").
class CaptureWriter
{
public:
	using EventFn = std::function<void(nlohmann::json)>;

	~CaptureWriter();

	void Start(EventFn on_event);
	void Stop(); // writes what is still queued, then returns

	void Push(std::unique_ptr<CaptureJob> job);
	size_t Queued() const; // jobs not written yet, including the one being written

private:
	void Loop();
	nlohmann::json Write(CaptureJob &job);

	EventFn on_event_;
	mutable std::mutex mutex_;
	std::condition_variable cond_;
	std::deque<std::unique_ptr<CaptureJob>> queue_;
	size_t in_progress_ = 0;
	bool stop_ = false;
	std::thread thread_;
};

} // namespace pycam
