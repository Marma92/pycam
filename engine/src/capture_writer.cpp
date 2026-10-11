#include "capture_writer.hpp"

#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <stdexcept>

#include <fcntl.h>
#include <unistd.h>

#include "image/image.hpp"

#include "dng_writer.hpp"

using nlohmann::json;
namespace fs = std::filesystem;

namespace pycam
{

namespace
{

// What the rpicam-apps image writers take (one entry per plane).
using Planes = std::vector<libcamera::Span<uint8_t>>;

// Leaves a core for the camera and the display while a look is applied (~0.6 s).
constexpr unsigned kLookThreads = 3;

double ms_since(std::chrono::steady_clock::time_point t)
{
	return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t).count();
}

// Waits until `path` (a file or a folder) is on the card, not only in the kernel's cache:
// on battery, a power cut would otherwise lose the last ~30 s of photos.
void sync_path(std::string const &path)
{
	int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		throw std::runtime_error("cannot open " + path + " to sync it: " + std::strerror(errno));
	int const ret = ::fsync(fd);
	int const err = errno;
	::close(fd);
	if (ret)
		throw std::runtime_error("cannot sync " + path + ": " + std::strerror(err));
}

// Write to "<name>.part", put it on the card, then rename (and put the rename on the card
// too): nobody ever sees a half-written file, and "capture_done" means "on the card".
template <typename SaveFn>
void save_atomically(std::string const &filename, SaveFn save)
{
	std::string tmp = filename + ".part";
	try
	{
		save(tmp);
		sync_path(tmp);
		fs::rename(tmp, filename);
		fs::path const parent = fs::path(filename).parent_path();
		sync_path(parent.empty() ? "." : parent.string());
	}
	catch (...)
	{
		std::error_code ignored;
		fs::remove(tmp, ignored);
		throw;
	}
}

} // namespace

CaptureWriter::~CaptureWriter()
{
	Stop();
}

void CaptureWriter::Start(EventFn on_event)
{
	on_event_ = std::move(on_event);
	thread_ = std::thread(&CaptureWriter::Loop, this);
}

void CaptureWriter::Stop()
{
	if (!thread_.joinable())
		return;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		stop_ = true;
	}
	cond_.notify_one();
	thread_.join();
}

void CaptureWriter::Push(std::unique_ptr<CaptureJob> job)
{
	{
		std::lock_guard<std::mutex> lock(mutex_);
		queue_.push_back(std::move(job));
	}
	cond_.notify_one();
}

size_t CaptureWriter::Queued() const
{
	std::lock_guard<std::mutex> lock(mutex_);
	return queue_.size() + in_progress_;
}

void CaptureWriter::Loop()
{
	for (;;)
	{
		std::unique_ptr<CaptureJob> job;
		{
			std::unique_lock<std::mutex> lock(mutex_);
			cond_.wait(lock, [this] { return stop_ || !queue_.empty(); });
			if (queue_.empty())
				return; // stop requested and nothing left to write
			job = std::move(queue_.front());
			queue_.pop_front();
			in_progress_ = 1;
		}
		json event = Write(*job);
		job.reset(); // free the ~37 MB before announcing that the slot is free
		{
			std::lock_guard<std::mutex> lock(mutex_);
			in_progress_ = 0;
			event["write_queue"] = queue_.size();
		}
		on_event_(std::move(event));
	}
}

json CaptureWriter::Write(CaptureJob &job)
{
	json files = json::array();
	json timings = json::object();
	try
	{
		fs::path parent = fs::path(job.base_path).parent_path();
		if (!parent.empty())
			fs::create_directories(parent);

		if (job.jpeg)
		{
			Planes mem { libcamera::Span<uint8_t>(job.yuv.data(), job.yuv.size()) };
			bool const look = job.look && !job.look->Empty();
			if (look && !job.base_jpeg_path.empty())
			{
				// The base, to apply another look later. Not announced as "file_saved": the
				// review is for the photo with its look.
				auto t = std::chrono::steady_clock::now();
				std::string filename = job.base_jpeg_path + ".JPG";
				save_atomically(filename, [&](std::string const &tmp)
								{ jpeg_save(mem, job.yuv_info, job.metadata, tmp, job.camera_model, job.options.get()); });
				timings["base_jpeg_ms"] = ms_since(t);
				files.push_back(filename);
			}
			if (look)
			{
				auto t = std::chrono::steady_clock::now();
				apply_look(*job.look, job.yuv.data(), job.yuv_info.width, job.yuv_info.height, job.yuv_info.stride,
						   job.id, kLookThreads);
				timings["look_ms"] = ms_since(t);
			}
			auto t = std::chrono::steady_clock::now();
			std::string filename = job.base_path + ".JPG";
			save_atomically(filename, [&](std::string const &tmp)
							{ jpeg_save(mem, job.yuv_info, job.metadata, tmp, job.camera_model, job.options.get()); });
			timings["jpeg_ms"] = ms_since(t);
			files.push_back(filename);
			// The JPEG alone is enough for the review: say so before writing the DNG.
			on_event_({ { "event", "file_saved" }, { "capture_id", job.id }, { "path", filename } });
		}
		if (job.dng)
		{
			auto t = std::chrono::steady_clock::now();
			std::string filename = job.base_path + ".DNG";
			DngOptions options { job.compress_dng, job.options->Get().exif };
			save_atomically(filename,
							[&](std::string const &tmp)
							{
								dng_write(libcamera::Span<const uint8_t>(job.raw.data(), job.raw.size()), job.raw_info,
										  job.metadata, tmp, job.camera_model, options);
							});
			timings["dng_ms"] = ms_since(t);
			files.push_back(filename);
			on_event_({ { "event", "file_saved" }, { "capture_id", job.id }, { "path", filename } });
		}
		return { { "event", "capture_done" }, { "capture_id", job.id }, { "files", files }, { "timings", timings } };
	}
	catch (std::exception const &e)
	{
		return { { "event", "capture_error" }, { "capture_id", job.id }, { "files", files }, { "error", e.what() } };
	}
}

} // namespace pycam
