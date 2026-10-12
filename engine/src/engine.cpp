#include "engine.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <stdexcept>

#include <libcamera/control_ids.h>

#include "core/buffer_sync.hpp"
#include "core/frame_info.hpp"
#include "image/image.hpp"

#include "analysis.hpp"
#include "control_codec.hpp"
#include "develop.hpp"
#include "dng_writer.hpp"

namespace controls = libcamera::controls;
using libcamera::Stream;
using nlohmann::json;

namespace pycam
{

namespace
{

constexpr auto kStatsPeriod = std::chrono::milliseconds(200);
// Viewfinder frame rate: 30 fps, down to 10 fps in low light. ZSL streams at full
// resolution (11.7 fps at best) and may slow down to 4 fps in low light.
constexpr int64_t kViewfinderMinFrameUs = 33'333;
constexpr int64_t kViewfinderMaxFrameUs = 100'000;
constexpr int64_t kZslMaxFrameUs = 250'000;

// Sensor modes. Viewfinder: 2x2 binning, full field of view. Stills: full resolution.
// Loupe: full resolution in 10 bits, the fastest full resolution mode (14 fps), so that
// the magnified image shows real pixels.
constexpr char kViewfinderMode[] = "2028:1520:12:P";
constexpr char kStillMode[] = "4056:3040:12:P";
constexpr char kLoupeMode[] = "4056:3040:10:P";
constexpr float kLoupeMaxZoom = 16;

// How long to wait for the first real frame after a start before stopping the camera
// anyway (a camera that delivers nothing does not deadlock either). See Run().
constexpr auto kStartupGrace = std::chrono::seconds(3);

// Stacks: 30 minutes of 1 s frames at most (the exposure time of the metadata is in
// microseconds, on 32 bits: 35 minutes).
constexpr unsigned kMaxStackFrames = 1800;
constexpr int64_t kMaxStackExposureUs = 30LL * 60 * 1'000'000;

std::string tuning_path(std::string const &dir, std::string const &name)
{
	if (dir.empty() || name.empty())
		return "";
	return (std::filesystem::path(dir) / (name + ".json")).string();
}

// A copy of `list` without the controls `drop`.
libcamera::ControlList without(libcamera::ControlList const &list, std::initializer_list<unsigned int> drop)
{
	libcamera::ControlList out;
	for (auto const &[id, value] : list)
		if (std::find(drop.begin(), drop.end(), id) == drop.end())
			out.set(id, value);
	return out;
}

// A command that cannot be executed; `code` is meant for the app, `what()` for humans.
struct CommandError : std::runtime_error
{
	CommandError(std::string c, std::string const &detail) : std::runtime_error(detail), code(std::move(c)) {}
	std::string code;
};

double ms_between(std::chrono::steady_clock::time_point from, std::chrono::steady_clock::time_point to)
{
	return std::chrono::duration<double, std::milli>(to - from).count();
}

bool valid_tuning_name(std::string const &name)
{
	return !name.empty() && name.size() < 64 &&
		   std::all_of(name.begin(), name.end(), [](unsigned char c) { return std::isalnum(c) || c == '-' || c == '_'; });
}

// libcamera reads the tuning file when the camera manager starts, from this variable.
// Returns the name of the tuning in use: empty for libcamera's own.
std::string export_tuning(std::string const &dir, std::string const &name)
{
	std::filesystem::path path = std::filesystem::path(dir) / (name + ".json");
	if (dir.empty() || name.empty() || !std::filesystem::exists(path))
	{
		if (!name.empty())
			std::cerr << "pycam-engine: no tuning file " << path << ", using libcamera's" << std::endl;
		unsetenv("LIBCAMERA_RPI_TUNING_FILE");
		return "";
	}
	setenv("LIBCAMERA_RPI_TUNING_FILE", path.c_str(), 1);
	return name;
}

std::string control_name(unsigned int id)
{
	auto it = controls::controls.find(id);
	return it != controls::controls.end() ? it->second->name() : "id " + std::to_string(id);
}

} // namespace

Engine::Engine(EngineConfig cfg)
	: cfg_(std::move(cfg)), tuning_(export_tuning(cfg_.tuning_dir, cfg_.tuning)), zsl_(cfg_.zsl),
	  still_denoise_(cfg_.still_denoise)
{
	tone_ = std::make_shared<const std::vector<uint8_t>>(tone_curve(tuning_path(cfg_.tuning_dir, tuning_)));
	if (cfg_.verbose < 2)
		setenv("LIBCAMERA_LOG_LEVELS", "*:ERROR", 0);

	// librpicam_app is configured through its command line options.
	std::vector<std::string> args = {
		"pycam-engine",
		"--nopreview",
		"--verbose", std::to_string(cfg_.verbose),
		"--viewfinder-width", std::to_string(cfg_.vf_width),
		"--viewfinder-height", std::to_string(cfg_.vf_height),
		"--lores-width", std::to_string(cfg_.lores_width),
		"--lores-height", std::to_string(cfg_.lores_height),
		"--viewfinder-mode", kViewfinderMode,
		"--mode", kStillMode,
	};
	std::vector<char *> argv;
	for (auto &arg : args)
		argv.push_back(arg.data());
	if (!app_.GetOptions()->Parse(argv.size(), argv.data()))
		throw std::runtime_error("invalid camera options");
}

int Engine::Run()
{
	ipc_.Start(cfg_.socket_path,
			   [this](int client, json message)
			   {
				   {
					   std::lock_guard<std::mutex> lock(commands_mutex_);
					   commands_.emplace_back(client, std::move(message), Clock::now());
				   }
				   // Wake the camera loop up: a "completed request" without a request.
				   PostToCameraLoop(RPiCamApp::MsgType::RequestComplete);
			   });
	writer_.Start([this](json event) { ipc_.Broadcast(event); });
	ticker_ = std::thread(
		[this]
		{
			while (!ticker_quit_)
			{
				std::this_thread::sleep_for(std::chrono::milliseconds(250));
				if (tick_)
					PostToCameraLoop(RPiCamApp::MsgType::RequestComplete);
			}
		});
	// Joined however Run() ends: a thread left joinable would abort the program.
	struct TickerStop
	{
		Engine *engine;
		~TickerStop()
		{
			engine->ticker_quit_ = true;
			engine->ticker_.join();
		}
	} ticker_stop { this };

	TryDisplay();

	app_.OpenCamera();
	FindCameraControls();
	Start(Phase::Viewfinder);

	std::cerr << "pycam-engine: ready, " << (zsl_ ? "zsl" : "switch") << " strategy, socket " << cfg_.socket_path
			  << "\n";
	ipc_.Broadcast({ { "event", "ready" }, { "state", State() } });

	// Never stop the camera before its first real frame since it started. libcamera (0.5+)
	// delivers the first frames flagged as startup frames, and rpicam-apps drops them on
	// the libcamera thread, taking the mutex that StopCamera() holds while it waits for
	// that very thread: stopping the camera then deadlocks (seen 2026-10-11, a capture
	// right after a restart). Once a real frame has come, the startup frames are over.
	auto may_stop_camera = [this] { return frames_since_start_ > 0 || Clock::now() - started_ > kStartupGrace; };

	for (;;)
	{
		if (quit_ && may_stop_camera())
			break;
		RPiCamApp::Msg msg = app_.Wait();
		if (msg.type == RPiCamApp::MsgType::Quit)
		{
			quit_ = true;
			continue; // leaves at the top of the loop, as soon as the camera may be stopped
		}
		if (msg.type == RPiCamApp::MsgType::Timeout)
		{
			std::cerr << "pycam-engine: camera timeout, restarting\n";
			ipc_.Broadcast({ { "event", "error" }, { "error", "camera_timeout" } });
			Restart(phase_);
			continue;
		}
		{
			CompletedRequestPtr request = std::move(std::get<CompletedRequestPtr>(msg.payload));
			if (request)
				OnFrame(request);
		} // the frame goes back to the camera here, before any reconfiguration

		DrainCommands();
		TryDisplay();

		if (reopen_ && may_stop_camera())
		{
			reopen_ = false;
			next_phase_.reset(); // Reopen() starts the viewfinder anyway
			Reopen();
		}
		else if (next_phase_ && may_stop_camera())
		{
			Phase phase = *next_phase_;
			next_phase_.reset();
			try
			{
				Restart(phase);
				if (phase == Phase::Still && pending_)
					// The exposure starts now: what a countdown on the screen needs.
					ipc_.Broadcast({ { "event", "exposing" },
									 { "capture_id", pending_->id },
									 { "exposure_us", ManualExposure() },
									 { "stack", pending_->stack != nullptr } });
			}
			catch (std::exception const &e)
			{
				if (phase != Phase::Still)
					throw;
				// Typically not enough CMA memory for the full resolution buffers: give up
				// this capture but keep the viewfinder alive.
				std::cerr << "pycam-engine: cannot switch to full resolution: " << e.what() << "\n";
				if (pending_)
					ipc_.Broadcast({ { "event", "capture_error" },
									 { "capture_id", pending_->id },
									 { "error", std::string("cannot switch to full resolution: ") + e.what() } });
				pending_.reset();
				Restart(Phase::Viewfinder);
			}
		}
		// Something to do as soon as the camera may stop: do not wait for a frame.
		tick_ = reopen_ || next_phase_.has_value();
	}

	std::cerr << "pycam-engine: stopping\n";
	player_.reset(); // its frames are on the display
	display_.reset(); // planes off, frames given back before the camera stops (see ReleaseFrames)
	app_.StopCamera();
	app_.Teardown();
	app_.CloseCamera();
	writer_.Stop(); // finish writing the photos already taken
	ipc_.Stop();
	return 0;
}

void Engine::TryDisplay()
{
	// Without the screen (still held by the boot splash, or plugged in later), keep trying
	// every couple of seconds; the application learns it from "display_ready".
	if (!cfg_.display || display_ || Clock::now() < next_display_try_)
		return;
	try
	{
		display_ = std::make_unique<Display>(cfg_.vf_width, cfg_.vf_height, cfg_.hud_shm,
											 [this](CompletedRequestPtr const &request, Stream *stream,
													StreamInfo const &info) { BeforeShow(request, stream, info); });
	}
	catch (std::exception const &e)
	{
		if (!display_failure_logged_)
			std::cerr << "pycam-engine: no display yet (" << e.what() << "), retrying\n";
		display_failure_logged_ = true;
		next_display_try_ = Clock::now() + std::chrono::seconds(2);
		return;
	}
	std::cerr << "pycam-engine: display ready\n";
	ipc_.Broadcast({ { "event", "display_ready" }, { "display", display_->Info() } });
}

void Engine::BeforeShow(CompletedRequestPtr const &request, Stream *stream, StreamInfo const &info)
{
	// Display thread. The viewfinder buffer only feeds the screen: drawing into it leaves
	// the photos untouched.
	std::string snapshot;
	std::shared_ptr<const Preview> preview;
	{
		std::lock_guard<std::mutex> lock(snapshot_mutex_);
		snapshot.swap(snapshot_path_);
		preview = preview_;
	}
	if (!preview && !focus_aids_.Active() && snapshot.empty())
		return;

	BufferWriteSync sync(&app_, request->buffers.at(stream));
	// The look first: the peaking and the zebras show on what the photo will look like.
	if (preview)
		apply_preview(*preview, sync.Get()[0].data(), info.width, info.height, info.stride);
	focus_aids_.Apply(sync.Get()[0].data(), info);

	if (!snapshot.empty())
	{
		try
		{
			StillOptions options;
			options.Set() = app_.GetOptions()->Get();
			jpeg_save(sync.Get(), info, request->metadata, snapshot, app_.CameraModel(), &options);
			ipc_.Broadcast({ { "event", "snapshot_done" }, { "path", snapshot } });
		}
		catch (std::exception const &e)
		{
			ipc_.Broadcast({ { "event", "snapshot_error" }, { "path", snapshot }, { "error", e.what() } });
		}
	}
}

void Engine::RequestQuit()
{
	quit_ = true;
	PostToCameraLoop(RPiCamApp::MsgType::Quit);
}

void Engine::PostToCameraLoop(RPiCamApp::MsgType type)
{
	RPiCamApp::MsgPayload payload = CompletedRequestPtr();
	app_.PostMessage(type, payload);
}

void Engine::Start(Phase phase)
{
	StillOptions *options = app_.GetOptions();
	bool const loupe = phase == Phase::Viewfinder && loupe_zoom_ > 1;
	if (loupe)
	{
		// A plain viewfinder, even in ZSL: the crop would also apply to the still stream.
		options->Set().viewfinder_mode = Mode(kLoupeMode);
		app_.ConfigureViewfinder();
	}
	else if (phase == Phase::Still)
	{
		// A stack needs a second buffer: with one, the camera has nowhere to put the next
		// frame while this one is added up, and drops every other frame (measured: 1 s
		// exposures 2 s apart).
		options->Set().buffer_count = pending_ && pending_->stack ? 2 : 1;
		options->Set().denoise = still_denoise_;
		options->Set().width = still_size_ ? (*still_size_)[0] : 0; // 0: full sensor
		options->Set().height = still_size_ ? (*still_size_)[1] : 0;
		app_.ConfigureStill(RPiCamApp::FLAG_STILL_RAW);
		options->Set().denoise = "auto";
	}
	else if (zsl_)
	{
		options->Set().buffer_count = cfg_.zsl_buffers;
		// cdn_hq would divide the ZSL frame rate by 3: the fast one instead.
		options->Set().denoise = still_denoise_ == "cdn_hq" ? cfg_.zsl_denoise : still_denoise_;
		options->Set().width = still_size_ ? (*still_size_)[0] : 0; // 0: full sensor
		options->Set().height = still_size_ ? (*still_size_)[1] : 0;
		app_.ConfigureZsl(RPiCamApp::FLAG_STILL_RAW);
		options->Set().denoise = "auto";
	}
	else
	{
		options->Set().viewfinder_mode = Mode(kViewfinderMode);
		app_.ConfigureViewfinder();
	}

	app_.SetControls(CameraControls(phase, phase == Phase::Viewfinder ? &viewfinder_exposure_ : nullptr));
	if (loupe)
		app_.SetControls(LoupeCrop()); // otherwise rpicam sets the full field of view
	app_.StartCamera();
	phase_ = phase;
	started_ = Clock::now();
	frames_since_start_ = 0;
}

int64_t Engine::ManualExposure() const
{
	if (user_controls_.get(controls::ExposureTimeMode).value_or(controls::ExposureTimeModeAuto) !=
		controls::ExposureTimeModeManual)
		return 0;
	return user_controls_.get(controls::ExposureTime).value_or(0);
}

libcamera::ControlList Engine::CameraControls(Phase phase, std::string *viewfinder) const
{
	// The photo: exactly what was asked, as long as it takes (no frame limits).
	if (viewfinder)
		viewfinder->clear();
	if (phase == Phase::Still)
		return user_controls_;

	// The viewfinder keeps at least 10 frames a second (4 in ZSL; the loupe is a plain
	// viewfinder). An exposure that fits is shown as it is; a longer manual one with a
	// manual gain is simulated: the longest frame, its gain raised as much; beyond the
	// sensor's gain, the viewfinder exposes automatically (the meter tells the rest).
	int64_t const longest = zsl_ && loupe_zoom_ <= 1 ? kZslMaxFrameUs : kViewfinderMaxFrameUs;
	int64_t const manual = ManualExposure();
	bool const manual_gain = user_controls_.get(controls::AnalogueGainMode).value_or(controls::AnalogueGainModeAuto) ==
							 controls::AnalogueGainModeManual;
	libcamera::ControlList list = user_controls_;
	int64_t frame_max = std::max(longest, manual);
	if (manual > longest && manual_gain)
	{
		float const gain = user_controls_.get(controls::AnalogueGain).value_or(1.0f) * manual / longest;
		if (gain <= max_gain_)
		{
			list.set(controls::ExposureTime, static_cast<int32_t>(longest));
			list.set(controls::AnalogueGain, gain);
			if (viewfinder)
				*viewfinder = "simulated";
		}
		else
		{
			list = without(list, { controls::ExposureTime.id(), controls::AnalogueGain.id() });
			list.set(controls::ExposureTimeMode, controls::ExposureTimeModeAuto);
			list.set(controls::AnalogueGainMode, controls::AnalogueGainModeAuto);
			if (viewfinder)
				*viewfinder = "auto";
		}
		frame_max = longest;
	}
	std::array<int64_t, 2> const limits { kViewfinderMinFrameUs, frame_max };
	list.set(controls::FrameDurationLimits, libcamera::Span<const int64_t, 2>(limits));
	return list;
}

void Engine::ReleaseFrames()
{
	// The display thread gives its frames back to the camera *before* it stops: a frame
	// given back goes through RPiCamApp::queueRequest(), which edits a set of requests
	// that StopCamera() clears without a lock (rpicam-apps 1.13). Both at once corrupted
	// the heap ("double free or corruption", 2026-10-12, a capture in switch mode while
	// the display thread was finishing a frame). It is the only other thread holding
	// frames, and this one hands it none until the camera starts again.
	if (display_)
		display_->Reset(); // the last image stays on the screen during the reconfiguration
}

void Engine::Restart(Phase phase)
{
	Clock::time_point t = Clock::now();
	ReleaseFrames();
	app_.StopCamera();
	app_.Teardown();
	Start(phase);
	reconfigure_ms_ = ms_between(t, started_);
}

void Engine::SetTuning()
{
	tuning_ = export_tuning(cfg_.tuning_dir, tuning_);
	tone_ = std::make_shared<const std::vector<uint8_t>>(tone_curve(tuning_path(cfg_.tuning_dir, tuning_)));
}

void Engine::FindCameraControls()
{
	camera_controls_ = nullptr;
	for (auto const &camera : app_.GetCameras())
		if (camera->id() == app_.CameraId())
			camera_controls_ = &camera->controls();
	if (camera_controls_)
	{
		auto it = camera_controls_->find(controls::AnalogueGain.id());
		if (it != camera_controls_->end())
			max_gain_ = it->second.max().get<float>();
	}
}

void Engine::Reopen()
{
	// A new tuning file: the camera manager must start again (CloseCamera() drops it).
	Clock::time_point t = Clock::now();
	ReleaseFrames();
	app_.StopCamera();
	app_.Teardown();
	app_.CloseCamera();
	SetTuning();
	app_.OpenCamera();
	FindCameraControls();
	Start(Phase::Viewfinder);
	reconfigure_ms_ = ms_between(t, started_);
	std::cerr << "pycam-engine: tuning " << (tuning_.empty() ? std::string("libcamera") : tuning_)
			  << ", camera reopened in " << int(reconfigure_ms_) << " ms\n";
	ipc_.Broadcast({ { "event", "tuning" },
					 { "tuning", tuning_.empty() ? json(nullptr) : json(tuning_) },
					 { "reopen_ms", reconfigure_ms_ } });
}

void Engine::OnFrame(CompletedRequestPtr &request)
{
	frames_since_start_++;
	if (request->framerate > 0)
		fps_ = request->framerate;

	if (phase_ == Phase::Still)
	{
		// The first full resolution frame is the photo; a stack takes them until it stops.
		if (pending_ && pending_->stack)
		{
			StackFrame(request);
			return;
		}
		if (pending_)
			GrabStill(request);
		next_phase_ = Phase::Viewfinder;
		return;
	}
	// ZSL: the photo is the first frame completed after the command was handled (there is
	// no still stream in the loupe: the capture command leaves it first).
	if (pending_ && zsl_ && app_.StillStream())
		GrabStill(request);

	// Before the display: in ZSL the statistics come from the viewfinder stream, on which
	// the display thread paints the peaking and the zebras.
	if (Clock::now() - last_stats_ >= kStatsPeriod)
		PublishStats(request);

	if (display_)
	{
		StreamInfo info;
		if (Stream *viewfinder = app_.ViewfinderStream(&info))
			display_->ShowFrame(request, viewfinder, info);
	}
}

std::unique_ptr<CaptureJob> Engine::NewJob(PendingCapture const &capture, CompletedRequestPtr &request)
{
	auto job = std::make_unique<CaptureJob>();
	job->id = capture.id;
	job->base_path = capture.base_path;
	job->jpeg = capture.jpeg;
	job->dng = capture.dng;
	job->compress_dng = capture.compress_dng;
	job->metadata = request->metadata;
	job->options = std::make_unique<StillOptions>();
	job->options->Set() = app_.GetOptions()->Get();
	job->options->Set().quality = capture.quality;
	job->options->Set().exif = capture.exif;
	job->camera_model = app_.CameraModel();
	job->look = look_;
	job->base_jpeg_path = capture.base_jpeg_path;
	return job;
}

void Engine::StackFrame(CompletedRequestPtr &request)
{
	PendingCapture &capture = *pending_;
	StreamInfo raw_info;
	Stream *raw = app_.RawStream(&raw_info);
	if (!raw)
		throw std::runtime_error("stack: no raw stream");
	{
		BufferReadSync sync(&app_, request->buffers[raw]);
		int const bits = raw_bits(raw_info);
		unsigned black = 4096u >> (16 - bits);
		if (auto levels = request->metadata.get(controls::SensorBlackLevels))
			black = static_cast<unsigned>((*levels)[0]) >> (16 - bits);
		capture.stack->Add(sync.Get()[0].data(), raw_info, black);
	}
	FrameInfo info(request);
	capture.stack_exposure_us += static_cast<int64_t>(info.exposure_time);
	unsigned const frames = capture.stack->frames();
	ipc_.Broadcast({ { "event", "stack_frame" },
					 { "capture_id", capture.id },
					 { "frames", frames },
					 { "exposure_us", capture.stack_exposure_us } });
	bool const last = capture.stop || (capture.stack_frames && frames >= capture.stack_frames) ||
					  frames >= kMaxStackFrames || capture.stack_exposure_us + info.exposure_time > kMaxStackExposureUs;
	if (!last)
		return;

	PendingCapture done = std::move(*pending_);
	pending_.reset();
	auto job = NewJob(done, request);
	app_.RawStream(&job->raw_info);
	app_.StillStream(&job->yuv_info);
	if (done.jpeg)
		job->yuv.resize(static_cast<size_t>(job->yuv_info.stride) * job->yuv_info.height * 3 / 2);
	job->stack = std::move(done.stack);
	job->tone = tone_;
	// The photo's exposure: all of them (mean, max: one of them).
	int64_t const exposure = job->stack->mode() == RawStack::Mode::Add ? done.stack_exposure_us : info.exposure_time;
	job->metadata.set(controls::ExposureTime, static_cast<int32_t>(std::min<int64_t>(exposure, INT32_MAX)));

	ipc_.Broadcast({ { "event", "captured" },
					 { "capture_id", done.id },
					 { "latency_ms", ms_between(done.t_command, Clock::now()) },
					 { "exposure_us", exposure },
					 { "frames", frames },
					 { "analogue_gain", info.analogue_gain },
					 { "digital_gain", info.digital_gain } });
	writer_.Push(std::move(job));
	next_phase_ = Phase::Viewfinder;
}

json Engine::CancelCapture()
{
	if (!pending_)
		return { { "cancelled", false } };
	uint64_t const id = pending_->id;
	pending_.reset();
	if (phase_ == Phase::Still || next_phase_)
		next_phase_ = Phase::Viewfinder;
	ipc_.Broadcast({ { "event", "capture_error" }, { "capture_id", id }, { "error", "cancelled" } });
	return { { "cancelled", true } };
}

void Engine::GrabStill(CompletedRequestPtr &request)
{
	PendingCapture capture = std::move(*pending_);
	pending_.reset();
	Clock::time_point t_frame = Clock::now();

	auto job = NewJob(capture, request);
	if (capture.jpeg)
	{
		Stream *still = app_.StillStream(&job->yuv_info);
		BufferReadSync sync(&app_, request->buffers[still]);
		auto const &planes = sync.Get();
		job->yuv.assign(planes[0].begin(), planes[0].end());
	}
	if (capture.dng)
	{
		Stream *raw = app_.RawStream(&job->raw_info);
		BufferReadSync sync(&app_, request->buffers[raw]);
		auto const &planes = sync.Get();
		job->raw.assign(planes[0].begin(), planes[0].end());
	}

	FrameInfo info(request);
	json event = {
		{ "event", "captured" },
		{ "capture_id", capture.id },
		{ "latency_ms", ms_between(capture.t_command, t_frame) },
		{ "copy_ms", ms_between(t_frame, Clock::now()) },
		{ "exposure_us", info.exposure_time },
		{ "analogue_gain", info.analogue_gain },
		{ "digital_gain", info.digital_gain },
	};
	if (!zsl_ || phase_ == Phase::Still)
	{
		event["reconfigure_ms"] = reconfigure_ms_;
		event["first_frame_ms"] = ms_between(started_, t_frame);
	}
	ipc_.Broadcast(event);
	writer_.Push(std::move(job));
}

void Engine::PublishStats(CompletedRequestPtr &request)
{
	last_stats_ = Clock::now();
	json event = { { "event", "stats" }, { "fps", fps_ } };

	StreamInfo stream_info;
	Stream *stream = app_.LoresStream(&stream_info);
	if (!stream) // ZSL has no lores stream
		stream = app_.ViewfinderStream(&stream_info);
	if (stream)
	{
		BufferReadSync sync(&app_, request->buffers[stream]);
		unsigned step = std::max(1u, stream_info.width / 160);
		FrameStats frame_stats =
			analyse_yuv420(sync.Get()[0].data(), stream_info.width, stream_info.height, stream_info.stride, step);
		event["mean_y"] = frame_stats.mean;
		event["histogram"] = frame_stats.luma;
		event["histogram_rgb"] = frame_stats.rgb;
	}

	FrameInfo frame(request);
	event["exposure_us"] = frame.exposure_time;
	event["analogue_gain"] = frame.analogue_gain;
	event["digital_gain"] = frame.digital_gain;
	event["colour_gains"] = frame.colour_gains;
	event["focus_fom"] = frame.focus;
	event["ae_converged"] = frame.aelock;
	if (!viewfinder_exposure_.empty())
		event["viewfinder"] = viewfinder_exposure_;
	auto const &metadata = request->metadata;
	if (auto lux = metadata.get(controls::Lux))
		event["lux"] = *lux;
	if (auto ct = metadata.get(controls::ColourTemperature))
		event["colour_temp"] = *ct;
	if (auto temp = metadata.get(controls::SensorTemperature))
		event["sensor_temp"] = *temp;
	ipc_.Broadcast(event);
}

void Engine::DrainCommands()
{
	std::deque<std::tuple<int, json, Clock::time_point>> commands;
	{
		std::lock_guard<std::mutex> lock(commands_mutex_);
		commands.swap(commands_);
	}
	for (auto const &[client, command, received] : commands)
		HandleCommand(client, command, received);
}

void Engine::HandleCommand(int client, json const &command, Clock::time_point received)
{
	json reply = { { "id", command.contains("id") ? command["id"] : json(nullptr) } };
	try
	{
		std::string const name = command.at("cmd").get<std::string>();
		if (name == "ping")
		{
		}
		else if (name == "get_state")
			reply["state"] = State();
		else if (name == "list_controls")
			reply["controls"] = ListControls();
		else if (name == "set_controls")
		{
			libcamera::ControlList list = json_to_controls(command.at("controls"));
			// libcamera ignores ExposureTime / AnalogueGain unless their mode is Manual:
			// setting a value implies manual mode for it.
			if (list.contains(controls::ExposureTime.id()) && !list.contains(controls::ExposureTimeMode.id()))
				list.set(controls::ExposureTimeMode, controls::ExposureTimeModeManual);
			if (list.contains(controls::AnalogueGain.id()) && !list.contains(controls::AnalogueGainMode.id()))
				list.set(controls::AnalogueGainMode, controls::AnalogueGainModeManual);
			for (auto const &[id, value] : list)
				if (camera_controls_ && camera_controls_->find(id) == camera_controls_->end())
					throw CommandError("unsupported", control_name(id) + " is not supported by this camera");
			remember_controls(user_controls_, list);
			// The exposure as the camera must run it now: the viewfinder may simulate a long
			// one, and a longer manual exposure (or the automatic one back) needs other
			// frame limits; until the next start, it would be cut short.
			std::initializer_list<unsigned int> const exposure_ids = {
				controls::ExposureTime.id(), controls::ExposureTimeMode.id(), controls::AnalogueGain.id(),
				controls::AnalogueGainMode.id(), controls::FrameDurationLimits.id()
			};
			if (std::any_of(exposure_ids.begin(), exposure_ids.end(), [&](unsigned int id) { return list.contains(id); }))
			{
				libcamera::ControlList camera =
					CameraControls(phase_, phase_ == Phase::Viewfinder ? &viewfinder_exposure_ : nullptr);
				list = without(list, exposure_ids);
				for (unsigned int id : exposure_ids)
					if (camera.contains(id))
						list.set(id, camera.get(id));
			}
			app_.SetControls(list);
		}
		else if (name == "capture")
			reply.update(Capture(command, received));
		else if (name == "capture_stop")
		{
			// A stack ends with the frame being exposed; a single exposure goes on.
			bool const stack = pending_ && pending_->stack;
			if (stack)
				pending_->stop = true;
			reply["stopping"] = stack;
		}
		else if (name == "capture_cancel")
			reply.update(CancelCapture());
		else if (name == "configure")
			Configure(command);
		else if (name == "focus_aids")
			focus_aids_.Configure(command);
		else if (name == "look")
		{
			SetLook(command);
			reply["look"] = LookState();
		}
		else if (name == "loupe")
		{
			Loupe(command);
			reply["loupe"] = LoupeState();
		}
		else if (name == "snapshot")
		{
			if (!display_)
				throw CommandError("unsupported", "no display");
			std::string path = command.at("path").get<std::string>();
			if (path.empty())
				throw CommandError("bad_request", "empty path");
			std::lock_guard<std::mutex> lock(snapshot_mutex_);
			snapshot_path_ = path;
		}
		else if (name == "video_play" || name == "video_pause" || name == "video_stop" || name == "display_power")
		{
			if (!display_)
				throw CommandError("unsupported", "no display");
			if (name == "display_power")
				display_->SetPower(command.at("on").get<bool>());
			else if (name == "video_play")
			{
				if (!player_)
					player_ = std::make_unique<VideoPlayer>(*display_, [this](json event) { ipc_.Broadcast(event); });
				try
				{
					reply["video"] = player_->Play(command.at("path").get<std::string>());
				}
				catch (std::runtime_error const &e)
				{
					throw CommandError("bad_request", e.what());
				}
			}
			else if (player_ && name == "video_pause")
				player_->Pause(command.value("paused", true));
			else if (player_)
				player_->Stop();
		}
		else if (name == "hud_info" || name == "hud_commit")
		{
			if (!display_)
				throw CommandError("unsupported", "no display");
			if (name == "hud_info")
				reply["display"] = display_->Info();
			else
				display_->CommitHud();
		}
		else if (name == "quit")
			quit_ = true;
		else
			throw CommandError("unknown_command", "unknown command " + name);
		reply["ok"] = true;
	}
	catch (CommandError const &e)
	{
		reply["ok"] = false;
		reply["error"] = e.code;
		reply["detail"] = e.what();
	}
	catch (std::exception const &e)
	{
		reply["ok"] = false;
		reply["error"] = "bad_request";
		reply["detail"] = e.what();
	}
	ipc_.Send(client, reply);
}

json Engine::Capture(json const &command, Clock::time_point received)
{
	if (pending_ || phase_ == Phase::Still || next_phase_)
		throw CommandError("busy", "a capture is already in progress");
	if (writer_.Queued() >= cfg_.max_queue)
		throw CommandError("busy", "write queue full");

	PendingCapture capture;
	capture.id = next_capture_id_++;
	capture.base_path = command.at("path").get<std::string>();
	if (capture.base_path.empty())
		throw CommandError("bad_request", "empty path");
	json formats = command.value("formats", json::array({ "jpeg", "dng" }));
	auto wants = [&](char const *format) { return std::find(formats.begin(), formats.end(), format) != formats.end(); };
	capture.jpeg = wants("jpeg");
	capture.dng = wants("dng");
	if (!capture.jpeg && !capture.dng)
		throw CommandError("bad_request", "formats must contain \"jpeg\" and/or \"dng\"");
	capture.compress_dng = command.value("dng_compression", false);
	capture.quality = command.value("quality", 93);
	capture.exif = command.value("exif", std::vector<std::string> {});
	capture.base_jpeg_path = command.value("base_jpeg", "");
	capture.t_command = received;
	if (command.contains("stack"))
	{
		json const &stack = command["stack"];
		try
		{
			capture.stack = std::make_shared<RawStack>(RawStack::ParseMode(stack.value("mode", "add")));
		}
		catch (std::invalid_argument const &e)
		{
			throw CommandError("bad_request", e.what());
		}
		capture.stack_frames = stack.value("frames", 0u);
		if (capture.stack_frames > kMaxStackFrames)
			throw CommandError("bad_request", "stack: " + std::to_string(kMaxStackFrames) + " frames at most");
	}

	uint64_t id = capture.id;
	pending_ = std::move(capture);

	// Like on any camera, the shutter leaves the loupe: the photo is always the full frame.
	bool const leave_loupe = loupe_zoom_ > 1;
	if (leave_loupe)
	{
		loupe_zoom_ = 1;
		ipc_.Broadcast({ { "event", "loupe" }, { "loupe", LoupeState() } });
	}
	// In ZSL, a stack and an exposure longer than its frames need the still mode too.
	if (!zsl_ || pending_->stack || ManualExposure() > kZslMaxFrameUs)
		next_phase_ = Phase::Still;
	else if (leave_loupe)
		next_phase_ = Phase::Viewfinder; // back to the ZSL configuration; its first frame is the photo
	return { { "capture_id", id } };
}

void Engine::Configure(json const &command)
{
	// Each key is optional. Stills only use these settings when they are taken (switch
	// strategy); in ZSL the still stream runs all the time, so a change restarts it.
	if (pending_ || phase_ == Phase::Still)
		throw CommandError("busy", "a capture is in progress");
	bool restart = false;
	if (command.contains("strategy"))
	{
		std::string strategy = command["strategy"].get<std::string>();
		if (strategy != "zsl" && strategy != "switch")
			throw CommandError("bad_request", "strategy must be \"zsl\" or \"switch\"");
		bool zsl = strategy == "zsl";
		restart |= zsl != zsl_;
		zsl_ = zsl;
	}
	if (command.contains("still_denoise"))
	{
		std::string denoise = command["still_denoise"].get<std::string>();
		if (denoise != "off" && denoise != "cdn_off" && denoise != "cdn_fast" && denoise != "cdn_hq")
			throw CommandError("bad_request", "still_denoise: off, cdn_off, cdn_fast or cdn_hq");
		restart |= zsl_ && denoise != still_denoise_;
		still_denoise_ = denoise;
	}
	if (command.contains("still_size"))
	{
		json const &size = command["still_size"];
		std::optional<std::array<unsigned, 2>> still_size;
		if (!size.is_null())
		{
			if (!size.is_array() || size.size() != 2)
				throw CommandError("bad_request", "still_size: [width, height], or null for the full sensor");
			unsigned w = size[0].get<unsigned>(), h = size[1].get<unsigned>();
			if (w < 64 || h < 64 || w > 4056 || h > 3040 || w % 2 || h % 2)
				throw CommandError("bad_request", "still_size: even, from 64x64 to 4056x3040");
			still_size = std::array<unsigned, 2> { w, h };
		}
		restart |= zsl_ && still_size != still_size_;
		still_size_ = still_size;
	}
	if (command.contains("tuning"))
	{
		// A file name (without .json) in the tuning folder, or null for libcamera's own.
		json const &value = command["tuning"];
		std::string tuning = value.is_null() ? "" : value.get<std::string>();
		if (!tuning.empty() &&
			(!valid_tuning_name(tuning) || cfg_.tuning_dir.empty() ||
			 !std::filesystem::exists(std::filesystem::path(cfg_.tuning_dir) / (tuning + ".json"))))
			throw CommandError("bad_request", "tuning: unknown tuning file " + tuning);
		if (tuning != tuning_)
		{
			tuning_ = tuning;
			reopen_ = true; // a few seconds without viewfinder
		}
	}
	if (restart)
		next_phase_ = Phase::Viewfinder;
}

void Engine::SetLook(json const &command)
{
	// The whole look at once: what is not given is off. Photos already taken keep theirs.
	auto look = std::make_shared<Look>();
	look->name = command.value("name", "");
	if (command.contains("lut") && !command["lut"].is_null())
	{
		try
		{
			look->lut = Lut3D::Load(command["lut"].get<std::string>());
		}
		catch (std::exception const &e)
		{
			throw CommandError("bad_request", e.what());
		}
	}
	look->vignette = std::clamp(command.value("vignette", 0.0f), 0.0f, 1.0f);
	look->grain = std::clamp(command.value("grain", 0.0f), 0.0f, 1.0f);
	look->grain_size = std::clamp(command.value("grain_size", 1.0f), 0.5f, 8.0f);
	look->grain_colour = std::clamp(command.value("grain_colour", 0.0f), 0.0f, 1.0f);
	look->grain_shadows = std::clamp(command.value("grain_shadows", 0.0f), 0.0f, 1.0f);
	look->grain_clumps = std::clamp(command.value("grain_clumps", 0.0f), 0.0f, 1.0f);
	look->halation = std::clamp(command.value("halation", 0.0f), 0.0f, 1.0f);
	look->halation_radius = std::clamp(command.value("halation_radius", 24.0f), 2.0f, 200.0f);
	look->halation_threshold = std::clamp(command.value("halation_threshold", 0.8f), 0.0f, 0.99f);
	if (command.contains("halation_colour"))
	{
		auto colour = command["halation_colour"].get<std::vector<float>>();
		if (colour.size() != 3)
			throw CommandError("bad_request", "halation_colour: [r, g, b], 0..1");
		for (unsigned i = 0; i < 3; i++)
			look->halation_colour[i] = std::clamp(colour[i], 0.0f, 1.0f);
	}
	std::shared_ptr<Preview> preview;
	if (command.contains("preview") && !command["preview"].is_null())
	{
		json const &p = command["preview"];
		auto luma = p.value("luma", std::vector<int> {});
		auto mix = p.value("luma_mix", std::vector<float> { 0, 0 });
		auto chroma = p.value("chroma", std::vector<float> { 1, 0, 0, 1 });
		if (luma.size() != 256 || mix.size() != 2 || chroma.size() != 4)
			throw CommandError("bad_request", "preview: luma (256 values), luma_mix [cb, cr], chroma [4 values]");
		preview = std::make_shared<Preview>();
		for (unsigned i = 0; i < 256; i++)
			preview->luma[i] = static_cast<uint8_t>(std::clamp(luma[i], 0, 255));
		preview->mix_cb = std::clamp(mix[0], -4.0f, 4.0f);
		preview->mix_cr = std::clamp(mix[1], -4.0f, 4.0f);
		for (unsigned i = 0; i < 4; i++)
			preview->chroma[i] = std::clamp(chroma[i], -4.0f, 4.0f);
	}
	look_ = std::move(look);
	std::lock_guard<std::mutex> lock(snapshot_mutex_); // the display thread reads it
	preview_ = std::move(preview);
}

json Engine::LookState() const
{
	return { { "name", look_->name },
			 { "lut", look_->lut ? json(look_->lut->size) : json(nullptr) },
			 { "vignette", look_->vignette },
			 { "grain", look_->grain },
			 { "grain_size", look_->grain_size },
			 { "grain_colour", look_->grain_colour },
			 { "grain_shadows", look_->grain_shadows },
			 { "grain_clumps", look_->grain_clumps },
			 { "halation", look_->halation },
			 { "halation_radius", look_->halation_radius },
			 { "preview", preview_ != nullptr } };
}

void Engine::Loupe(json const &command)
{
	float zoom = command.value("zoom", loupe_zoom_);
	if (!(zoom >= 1 && zoom <= kLoupeMaxZoom))
		throw CommandError("bad_request", "zoom: between 1 (off) and 16");
	if (pending_ || phase_ == Phase::Still)
		throw CommandError("busy", "a capture is in progress");
	if (command.contains("center"))
	{
		json const &center = command["center"];
		if (!center.is_array() || center.size() != 2)
			throw CommandError("bad_request", "center: [x, y], fractions of the image");
		loupe_x_ = std::clamp(center[0].get<float>(), 0.0f, 1.0f);
		loupe_y_ = std::clamp(center[1].get<float>(), 0.0f, 1.0f);
	}
	bool const was_on = loupe_zoom_ > 1;
	loupe_zoom_ = zoom;
	if (was_on != (zoom > 1))
		next_phase_ = Phase::Viewfinder; // sensor mode change: reconfigure
	else if (zoom > 1)
		app_.SetControls(LoupeCrop()); // same mode: the crop follows within a few frames
}

libcamera::ControlList Engine::LoupeCrop() const
{
	// ScalerCrop is in sensor pixels; its maximum is the whole sensor area.
	auto const max = camera_controls_->at(&controls::ScalerCrop).max().get<libcamera::Rectangle>();
	unsigned const w = static_cast<unsigned>(max.width / loupe_zoom_) & ~1u;
	unsigned const h = static_cast<unsigned>(max.height / loupe_zoom_) & ~1u;
	int const x = std::clamp(int(loupe_x_ * max.width - w / 2.0f), 0, int(max.width - w));
	int const y = std::clamp(int(loupe_y_ * max.height - h / 2.0f), 0, int(max.height - h));
	libcamera::ControlList crop;
	crop.set(controls::ScalerCrop, libcamera::Rectangle(max.x + (x & ~1), max.y + (y & ~1), w, h));
	return crop;
}

json Engine::LoupeState() const
{
	return { { "zoom", loupe_zoom_ }, { "center", { loupe_x_, loupe_y_ } } };
}

json Engine::State() const
{
	json streams = json::object();
	auto add = [&](char const *name, Stream *stream)
	{
		if (!stream)
			return;
		StreamInfo info = app_.GetStreamInfo(stream);
		streams[name] = { info.width, info.height };
	};
	add("viewfinder", app_.ViewfinderStream());
	add("lores", app_.LoresStream());
	add("still", app_.StillStream());
	add("raw", app_.RawStream());

	return {
		{ "camera", app_.CameraModel() },
		{ "strategy", zsl_ ? "zsl" : "switch" },
		{ "phase", phase_ == Phase::Still ? "still" : "viewfinder" },
		{ "viewfinder_exposure", viewfinder_exposure_.empty() ? json(nullptr) : json(viewfinder_exposure_) },
		{ "fps", fps_ },
		{ "write_queue", writer_.Queued() },
		{ "max_queue", cfg_.max_queue },
		{ "streams", streams },
		{ "controls", controls_to_json(user_controls_) },
		{ "display", display_ ? display_->Info() : json(nullptr) },
		{ "focus_aids", focus_aids_.State() },
		{ "loupe", LoupeState() },
		{ "still_denoise", still_denoise_ },
		{ "still_size", still_size_ ? json(*still_size_) : json(nullptr) },
		{ "tuning", tuning_.empty() ? json(nullptr) : json(tuning_) },
		{ "look", LookState() },
		{ "video", player_ ? player_->State() : json(nullptr) },
	};
}

json Engine::ListControls() const
{
	json out = json::object();
	if (!camera_controls_)
		return out;
	for (auto const &[id, info] : *camera_controls_)
	{
		json entry = {
			{ "min", control_value_to_json(info.min()) },
			{ "max", control_value_to_json(info.max()) },
			{ "default", control_value_to_json(info.def()) },
		};
		if (!id->enumerators().empty())
		{
			json values = json::object();
			for (auto const &[value, enumerator] : id->enumerators())
				values[enumerator] = value;
			entry["values"] = values;
		}
		out[id->name()] = entry;
	}
	return out;
}

} // namespace pycam
