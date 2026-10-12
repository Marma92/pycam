#include "recorder.hpp"

#include <cstdio>
#include <ctime>
#include <filesystem>
#include <stdexcept>
#include <vector>

#include <fcntl.h>
#include <unistd.h>

#include "core/buffer_sync.hpp"

extern "C"
{
#include <libavformat/avformat.h>
}

using nlohmann::json;

namespace pycam
{

namespace
{

constexpr AVRational kMicroseconds { 1, 1000000 };

void sync_path(std::string const &path)
{
	int fd = open(path.c_str(), O_RDONLY);
	if (fd >= 0)
	{
		fsync(fd);
		close(fd);
	}
}

// The SPS and PPS at the start of a key frame (Annex B, as the encoder writes them with
// inline headers): what the MP4 needs in its header.
std::vector<uint8_t> parameter_sets(uint8_t const *data, size_t size)
{
	std::vector<uint8_t> out;
	size_t i = 0;
	auto start_code = [&](size_t at) { return at + 3 < size && data[at] == 0 && data[at + 1] == 0 && data[at + 2] == 1; };
	while (i < size)
	{
		if (!start_code(i))
		{
			i++;
			continue;
		}
		size_t const nal = i + 3;
		size_t end = nal;
		while (end < size && !start_code(end))
			end++;
		size_t nal_end = end;
		while (nal_end > nal && data[nal_end - 1] == 0) // the leading zero of a 4 byte start code
			nal_end--;
		int const type = data[nal] & 0x1f;
		if (type == 7 || type == 8)
		{
			static uint8_t const code[4] = { 0, 0, 0, 1 };
			out.insert(out.end(), code, code + 4);
			out.insert(out.end(), data + nal, data + nal_end);
		}
		else if (type == 1 || type == 5)
			break; // the picture itself
		i = end;
	}
	return out;
}

} // namespace

Recorder::Recorder(std::string path, StreamInfo const &info, unsigned fps, unsigned bitrate, EventFn on_event)
	: path_(std::move(path)), part_(path_ + ".part"), info_(info), fps_(fps), on_event_(std::move(on_event))
{
	// The encoder's settings, set directly: parsing them as a command line would make rpicam
	// restart its camera manager, under the running camera.
	auto &o = options_.Set();
	o.codec = "h264";
	o.bitrate.set(std::to_string(bitrate));
	o.profile = "high";
	o.level = "4.1";
	o.intra = fps; // a key frame each second
	o.inline_headers = true; // the SPS/PPS before each key frame: the MP4 header takes them
	o.framerate = float(fps);
	o.width = info.width; // the encoded stream's size: from the options, not the stream
	o.height = info.height;

	std::filesystem::path parent = std::filesystem::path(path_).parent_path();
	if (!parent.empty())
		std::filesystem::create_directories(parent);
	if (avformat_alloc_output_context2(&format_, nullptr, "mp4", part_.c_str()) < 0 || !format_)
		throw std::runtime_error("cannot make an MP4");
	stream_ = avformat_new_stream(format_, nullptr);
	if (!stream_)
		throw std::runtime_error("cannot make an MP4 stream");
	stream_->codecpar->codec_type = AVMEDIA_TYPE_VIDEO;
	stream_->codecpar->codec_id = AV_CODEC_ID_H264;
	stream_->codecpar->width = info.width;
	stream_->codecpar->height = info.height;
	stream_->time_base = { 1, 90000 };
	stream_->avg_frame_rate = { int(fps), 1 };
	if (avio_open(&format_->pb, part_.c_str(), AVIO_FLAG_WRITE) < 0)
	{
		avformat_free_context(format_);
		format_ = nullptr;
		throw std::runtime_error("cannot write " + part_);
	}

	try
	{
		encoder_.reset(Encoder::Create(&options_, info));
		if (!encoder_)
			throw std::runtime_error("no H.264 encoder");
	}
	catch (...)
	{
		avio_closep(&format_->pb);
		avformat_free_context(format_);
		format_ = nullptr;
		std::remove(part_.c_str());
		throw;
	}
	encoder_->SetInputDoneCallback([this](void *mem) { OnInputDone(mem); });
	encoder_->SetOutputReadyCallback([this](void *mem, size_t size, int64_t timestamp_us, bool keyframe)
									 { OnOutput(mem, size, timestamp_us, keyframe); });
}

Recorder::~Recorder()
{
	Stop();
}

void Recorder::Encode(CompletedRequestPtr const &request, libcamera::Stream *stream, RPiCamApp &app)
{
	libcamera::FrameBuffer *buffer = request->buffers.at(stream);
	BufferReadSync sync(&app, buffer);
	libcamera::Span<uint8_t> span = sync.Get()[0];
	int64_t const timestamp_us = buffer->metadata().timestamp / 1000;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		encoding_.push_back(request);
	}
	encoder_->EncodeBuffer(buffer->planes()[0].fd.get(), span.size(), span.data(), info_, timestamp_us);
}

void Recorder::OnInputDone(void *)
{
	// The encoder is done with the oldest frame: back to the camera.
	CompletedRequestPtr done;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (!encoding_.empty())
		{
			done = std::move(encoding_.front());
			encoding_.pop_front();
		}
	}
}

void Recorder::StartFile(uint8_t const *data, size_t size)
{
	std::vector<uint8_t> headers = parameter_sets(data, size);
	if (headers.empty())
		throw std::runtime_error("no SPS/PPS in the first key frame");
	stream_->codecpar->extradata = static_cast<uint8_t *>(av_mallocz(headers.size() + AV_INPUT_BUFFER_PADDING_SIZE));
	std::copy(headers.begin(), headers.end(), stream_->codecpar->extradata);
	stream_->codecpar->extradata_size = headers.size();
	// When it was shot (UTC): galleries and clouds sort videos by it, not by the date the
	// file was copied.
	std::time_t const now = std::time(nullptr);
	std::tm utc {};
	gmtime_r(&now, &utc);
	char creation[32];
	std::strftime(creation, sizeof(creation), "%Y-%m-%dT%H:%M:%S.000000Z", &utc);
	av_dict_set(&format_->metadata, "creation_time", creation, 0);
	if (avformat_write_header(format_, nullptr) < 0)
		throw std::runtime_error("cannot write the MP4 header");
	started_ = true;
}

void Recorder::OnOutput(void *mem, size_t size, int64_t timestamp_us, bool keyframe)
{
	if (!error_.empty())
		return;
	try
	{
		auto const *data = static_cast<uint8_t const *>(mem);
		if (!started_)
		{
			if (!keyframe)
				return; // the file starts at a key frame, with its parameter sets
			StartFile(data, size);
			first_us_ = timestamp_us;
		}
		AVPacket *packet = av_packet_alloc();
		if (!packet || av_new_packet(packet, size) < 0)
		{
			av_packet_free(&packet);
			throw std::runtime_error("out of memory");
		}
		std::copy(data, data + size, packet->data);
		packet->pts = packet->dts = av_rescale_q(timestamp_us - first_us_, kMicroseconds, stream_->time_base);
		packet->duration = av_rescale_q(1000000 / fps_, kMicroseconds, stream_->time_base);
		packet->stream_index = stream_->index;
		if (keyframe)
			packet->flags |= AV_PKT_FLAG_KEY;
		int const err = av_write_frame(format_, packet);
		av_packet_free(&packet);
		if (err < 0)
			throw std::runtime_error("cannot write the video (card full?)");
		{
			std::lock_guard<std::mutex> lock(mutex_);
			frames_++;
			bytes_ += size;
			last_us_ = timestamp_us;
			duration_ = (timestamp_us - first_us_) / 1e6;
		}
		if (timestamp_us - last_progress_us_ >= 1000000)
		{
			last_progress_us_ = timestamp_us;
			on_event_({ { "event", "record_progress" },
						{ "path", path_ },
						{ "duration", (timestamp_us - first_us_) / 1e6 },
						{ "bytes", bytes_ } });
		}
	}
	catch (std::exception const &e)
	{
		error_ = e.what();
		on_event_({ { "event", "record_error" }, { "path", path_ }, { "error", error_ } });
	}
}

json Recorder::Stop()
{
	if (stopped_)
		return nullptr;
	stopped_ = true;
	// The encoder first: its threads end, nothing comes out any more. The frames it still
	// held go back to the camera here, on the camera loop thread.
	encoder_.reset();
	{
		std::lock_guard<std::mutex> lock(mutex_);
		encoding_.clear();
	}
	json result = { { "path", path_ }, { "frames", frames_ }, { "bytes", bytes_ } };
	double const duration = started_ ? (last_us_ - first_us_) / 1e6 + 1.0 / fps_ : 0;
	result["duration"] = duration;
	if (format_)
	{
		if (started_ && av_write_trailer(format_) < 0 && error_.empty())
			error_ = "cannot end the MP4";
		avio_closep(&format_->pb);
		avformat_free_context(format_);
		format_ = nullptr;
	}
	if (!started_ && error_.empty())
		error_ = "no frame recorded";
	if (error_.empty())
	{
		sync_path(part_);
		std::filesystem::rename(part_, path_);
		sync_path(std::filesystem::path(path_).parent_path().string());
	}
	else
	{
		std::remove(part_.c_str());
		result["error"] = error_;
	}
	return result;
}

json Recorder::State() const
{
	std::lock_guard<std::mutex> lock(mutex_);
	return { { "path", path_ }, { "frames", frames_ }, { "bytes", bytes_ }, { "duration", duration_ } };
}

} // namespace pycam
