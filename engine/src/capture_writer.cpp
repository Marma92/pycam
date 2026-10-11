#include "capture_writer.hpp"

#include <chrono>
#include <filesystem>

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

double ms_since(std::chrono::steady_clock::time_point t)
{
	return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t).count();
}

// Write to "<name>.part" then rename, so that nobody ever sees a half-written file.
template <typename SaveFn>
void save_atomically(std::string const &filename, SaveFn save)
{
	std::string tmp = filename + ".part";
	try
	{
		save(tmp);
		fs::rename(tmp, filename);
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
			auto t = std::chrono::steady_clock::now();
			std::string filename = job.base_path + ".JPG";
			Planes mem { libcamera::Span<uint8_t>(job.yuv.data(), job.yuv.size()) };
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
