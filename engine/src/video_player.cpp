#include "video_player.hpp"

#include <chrono>
#include <iostream>
#include <stdexcept>
#include <vector>

extern "C"
{
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext_drm.h>
}

using nlohmann::json;

namespace pycam
{

namespace
{

using Clock = std::chrono::steady_clock;
constexpr auto kStopTimeout = std::chrono::seconds(3);

struct Stopped // Stop() was called: out of the decoding loop
{
};

std::string av_error(int err)
{
	char text[AV_ERROR_MAX_STRING_SIZE] = {};
	av_strerror(err, text, sizeof(text));
	return text;
}

// The decoder's own buffers or nothing: software frames would need copying, which the
// Pi 3 does at ~20 frames a second only.
AVPixelFormat drm_prime_only(AVCodecContext *, AVPixelFormat const *formats)
{
	for (AVPixelFormat const *p = formats; *p != AV_PIX_FMT_NONE; p++)
		if (*p == AV_PIX_FMT_DRM_PRIME)
			return *p;
	return AV_PIX_FMT_NONE;
}

} // namespace

struct VideoPlayer::FreeQueue
{
	std::mutex mutex;
	std::vector<AVFrame *> frames;

	void Push(AVFrame *frame)
	{
		std::lock_guard<std::mutex> lock(mutex);
		frames.push_back(frame);
	}
	void Drain()
	{
		std::vector<AVFrame *> done;
		{
			std::lock_guard<std::mutex> lock(mutex);
			done.swap(frames);
		}
		for (AVFrame *frame : done)
			av_frame_free(&frame);
	}
	~FreeQueue() { Drain(); }
};

struct VideoPlayer::Decoder
{
	AVFormatContext *format = nullptr;
	AVCodecContext *codec = nullptr;
	AVPacket *packet = nullptr;
	AVFrame *frame = nullptr;
	int stream = -1;

	~Decoder()
	{
		// Frames still on the screen keep the decoder's buffers (and device) alive.
		av_frame_free(&frame);
		av_packet_free(&packet);
		avcodec_free_context(&codec);
		avformat_close_input(&format);
	}
};

VideoPlayer::VideoPlayer(Display &display, EventFn on_event)
	: display_(display), on_event_(std::move(on_event)), free_(std::make_shared<FreeQueue>())
{
}

VideoPlayer::~VideoPlayer()
{
	Stop();
}

json VideoPlayer::Play(std::string const &path)
{
	Stop();
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (stuck_)
			throw std::runtime_error("the video decoder is stuck: restart the engine");
	}
	free_->Drain(); // the last video's frames, now off the screen
	auto d = std::make_unique<Decoder>();
	int err = avformat_open_input(&d->format, path.c_str(), nullptr, nullptr);
	if (err < 0)
		throw std::runtime_error("cannot open " + path + ": " + av_error(err));
	if ((err = avformat_find_stream_info(d->format, nullptr)) < 0)
		throw std::runtime_error("cannot read " + path + ": " + av_error(err));
	d->stream = av_find_best_stream(d->format, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
	if (d->stream < 0)
		throw std::runtime_error("no video in " + path);
	AVStream const *stream = d->format->streams[d->stream];
	AVCodec const *codec =
		stream->codecpar->codec_id == AV_CODEC_ID_H264 ? avcodec_find_decoder_by_name("h264_v4l2m2m") : nullptr;
	if (!codec)
		throw std::runtime_error("only H.264 videos can be played (the VideoCore's decoder)");
	d->codec = avcodec_alloc_context3(codec);
	if (!d->codec || avcodec_parameters_to_context(d->codec, stream->codecpar) < 0)
		throw std::runtime_error("cannot set the decoder up");
	d->codec->get_format = drm_prime_only;
	if ((err = avcodec_open2(d->codec, codec, nullptr)) < 0)
		throw std::runtime_error("cannot open the decoder: " + av_error(err));
	d->packet = av_packet_alloc();
	d->frame = av_frame_alloc();
	if (!d->packet || !d->frame)
		throw std::runtime_error("out of memory");

	double duration = d->format->duration > 0 ? d->format->duration / double(AV_TIME_BASE)
											  : stream->duration * av_q2d(stream->time_base);
	json info = { { "path", path },
				  { "duration", duration },
				  { "width", stream->codecpar->width },
				  { "height", stream->codecpar->height },
				  { "fps", av_q2d(stream->avg_frame_rate) } };
	uint64_t generation;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		stop_ = paused_ = false;
		active_ = true;
		path_ = path;
		position_ = 0;
		duration_ = duration;
		generation = ++generation_;
	}
	display_.SetExternal(true);
	std::promise<void> done;
	finished_ = done.get_future();
	thread_ = std::thread(&VideoPlayer::Run, this, d.release(), generation, std::move(done));
	return info;
}

void VideoPlayer::Pause(bool paused)
{
	{
		std::lock_guard<std::mutex> lock(mutex_);
		paused_ = paused;
	}
	cond_.notify_all();
}

void VideoPlayer::Stop()
{
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (!active_)
			return;
		stop_ = true;
	}
	cond_.notify_all();
	bool stuck = false;
	if (thread_.joinable())
	{
		// The camera loop calls this: it must not hang on a decoder that does.
		if (finished_.wait_for(kStopTimeout) == std::future_status::ready)
			thread_.join();
		else
		{
			std::cerr << "pycam-engine: video: the decoder does not answer, left behind\n";
			thread_.detach();
			stuck = true;
		}
	}
	display_.SetExternal(false);
	std::lock_guard<std::mutex> lock(mutex_);
	active_ = false;
	stuck_ = stuck_ || stuck;
}

json VideoPlayer::State() const
{
	std::lock_guard<std::mutex> lock(mutex_);
	if (!active_)
		return nullptr;
	return { { "path", path_ }, { "position", position_ }, { "duration", duration_ }, { "paused", paused_ } };
}

void VideoPlayer::Run(Decoder *raw, uint64_t generation, std::promise<void> done)
{
	std::unique_ptr<Decoder> d(raw);
	std::shared_ptr<FreeQueue> const free = free_;
	double const time_base = av_q2d(d->format->streams[d->stream]->time_base);
	double duration;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		duration = duration_;
	}
	bool started = false;
	double first = 0, position = 0;
	Clock::time_point t0 {}, last_progress {};

	// One decoded frame on the screen at its time; false: stop.
	auto show = [&](AVFrame *frame) -> bool
	{
		if (frame->format != AV_PIX_FMT_DRM_PRIME)
			throw std::runtime_error("the decoder gives no DRM PRIME frames");
		double const pts =
			(frame->best_effort_timestamp == AV_NOPTS_VALUE ? 0 : frame->best_effort_timestamp) * time_base;
		if (!started)
		{
			first = pts;
			t0 = Clock::now();
			started = true;
		}
		{
			std::unique_lock<std::mutex> lock(mutex_);
			for (;;)
			{
				if (stop_ || generation != generation_)
					return false;
				if (paused_)
				{
					Clock::time_point const paused_at = Clock::now();
					// Woken now and then to give back the frames the display is done with.
					cond_.wait_for(lock, std::chrono::milliseconds(200));
					t0 += Clock::now() - paused_at;
					lock.unlock();
					free->Drain();
					lock.lock();
					continue;
				}
				auto const due = t0 + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(pts - first));
				if (Clock::now() >= due)
					break;
				cond_.wait_until(lock, due, [&] { return stop_ || paused_ || generation != generation_; });
			}
			position = position_ = pts - first;
		}

		auto const *desc = reinterpret_cast<AVDRMFrameDescriptor const *>(frame->data[0]);
		if (desc->nb_objects != 1 || desc->nb_layers != 1 || desc->layers[0].nb_planes > 4)
			throw std::runtime_error("decoder buffers laid out in an unexpected way");
		AVFrame *ref = av_frame_clone(frame);
		if (!ref)
			throw std::runtime_error("out of memory");
		auto external = std::make_shared<Display::ExternalFrame>();
		external->owner = std::shared_ptr<void>(ref, [free](void *p) { free->Push(static_cast<AVFrame *>(p)); });
		external->fd = desc->objects[0].fd;
		external->modifier = desc->objects[0].format_modifier;
		AVDRMLayerDescriptor const &layer = desc->layers[0];
		external->format = layer.format;
		external->nb_planes = layer.nb_planes;
		for (int p = 0; p < layer.nb_planes; p++)
		{
			external->offsets[p] = layer.planes[p].offset;
			external->pitches[p] = layer.planes[p].pitch;
		}
		external->width = frame->width;
		external->height = frame->height;
		// Unspecified (our timelapses): BT.709, limited range, as for any HD video.
		external->bt709 = frame->colorspace != AVCOL_SPC_BT470BG && frame->colorspace != AVCOL_SPC_SMPTE170M;
		external->full_range = frame->color_range == AVCOL_RANGE_JPEG;
		display_.ShowExternal(std::move(external));

		if (Clock::now() - last_progress >= std::chrono::milliseconds(500))
		{
			last_progress = Clock::now();
			on_event_({ { "event", "video_progress" }, { "position", position }, { "duration", duration } });
		}
		return true;
	};

	// Frames out of the decoder until it needs more input (or has no more).
	auto receive = [&]() -> int
	{
		for (;;)
		{
			free->Drain();
			int err = avcodec_receive_frame(d->codec, d->frame);
			if (err == AVERROR(EAGAIN) || err == AVERROR_EOF)
				return err;
			if (err < 0)
				throw std::runtime_error("decoding: " + av_error(err));
			bool const go = show(d->frame);
			av_frame_unref(d->frame);
			if (!go)
				throw Stopped {};
		}
	};

	std::string reason = "end", error;
	try
	{
		bool flushing = false;
		for (;;)
		{
			AVPacket *packet = nullptr; // null: flush the decoder (end of file)
			if (!flushing)
			{
				if (av_read_frame(d->format, d->packet) < 0)
					flushing = true;
				else if (d->packet->stream_index == d->stream)
					packet = d->packet;
				else
				{
					av_packet_unref(d->packet);
					continue;
				}
			}
			// The decoder may take nothing more until frames come out of it.
			int err;
			while ((err = avcodec_send_packet(d->codec, packet)) == AVERROR(EAGAIN))
				if (receive() == AVERROR(EAGAIN))
					std::this_thread::sleep_for(std::chrono::milliseconds(2));
			if (packet)
				av_packet_unref(packet);
			if (err < 0 && err != AVERROR_EOF)
				throw std::runtime_error("decoding: " + av_error(err));
			if (receive() == AVERROR_EOF)
				break;
			if (flushing)
				std::this_thread::sleep_for(std::chrono::milliseconds(2)); // the last frames are coming
		}
	}
	catch (Stopped const &)
	{
		reason = "stopped";
	}
	catch (std::exception const &e)
	{
		reason = "error";
		error = e.what();
	}
	free->Drain();
	bool current;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		current = generation == generation_;
	}
	if (current)
	{
		json event = { { "event", "video_ended" }, { "reason", reason }, { "position", position } };
		if (!error.empty())
			event["error"] = error;
		on_event_(event);
	}
	d.reset(); // the last frame stays on the screen (and its buffer alive) until Stop()
	done.set_value();
}

} // namespace pycam
