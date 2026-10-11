#include "engine.hpp"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <iostream>
#include <stdexcept>

#include <libcamera/control_ids.h>

#include "core/buffer_sync.hpp"
#include "core/frame_info.hpp"

#include "analysis.hpp"
#include "control_codec.hpp"

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

std::string control_name(unsigned int id)
{
	auto it = controls::controls.find(id);
	return it != controls::controls.end() ? it->second->name() : "id " + std::to_string(id);
}

} // namespace

Engine::Engine(EngineConfig cfg) : cfg_(std::move(cfg)), zsl_(cfg_.zsl)
{
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
		// Binned mode (full field of view) for the viewfinder, full resolution for stills.
		"--viewfinder-mode", "2028:1520:12:P",
		"--mode", "4056:3040:12:P",
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

	TryDisplay();

	app_.OpenCamera();
	for (auto const &camera : app_.GetCameras())
		if (camera->id() == app_.CameraId())
			camera_controls_ = &camera->controls();
	Start(Phase::Viewfinder);

	std::cerr << "pycam-engine: ready, " << (zsl_ ? "zsl" : "switch") << " strategy, socket " << cfg_.socket_path
			  << "\n";
	ipc_.Broadcast({ { "event", "ready" }, { "state", State() } });

	while (!quit_)
	{
		RPiCamApp::Msg msg = app_.Wait();
		if (msg.type == RPiCamApp::MsgType::Quit)
			break;
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

		if (next_phase_)
		{
			Phase phase = *next_phase_;
			next_phase_.reset();
			try
			{
				Restart(phase);
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
	}

	std::cerr << "pycam-engine: stopping\n";
	app_.StopCamera();
	display_.reset(); // planes off, frames given back, before the buffers are freed
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
		display_ = std::make_unique<Display>(cfg_.vf_width, cfg_.vf_height, cfg_.hud_shm);
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

	// A manual exposure longer than the viewfinder frame must still be honoured.
	int64_t manual_exposure = 0;
	if (user_controls_.get(controls::ExposureTimeMode).value_or(controls::ExposureTimeModeAuto) ==
		controls::ExposureTimeModeManual)
		manual_exposure = user_controls_.get(controls::ExposureTime).value_or(0);

	std::optional<std::array<int64_t, 2>> frame_limits;
	if (zsl_)
	{
		options->Set().buffer_count = cfg_.zsl_buffers;
		options->Set().denoise = cfg_.zsl_denoise;
		app_.ConfigureZsl(RPiCamApp::FLAG_STILL_RAW);
		options->Set().denoise = "auto";
		frame_limits = { kViewfinderMinFrameUs, std::max(kZslMaxFrameUs, manual_exposure) };
	}
	else if (phase == Phase::Still)
	{
		options->Set().buffer_count = 1;
		options->Set().denoise = cfg_.still_denoise;
		app_.ConfigureStill(RPiCamApp::FLAG_STILL_RAW);
		options->Set().denoise = "auto";
		// No frame limits: rpicam lets a still exposure last as long as needed.
	}
	else
	{
		app_.ConfigureViewfinder();
		frame_limits = { kViewfinderMinFrameUs, std::max(kViewfinderMaxFrameUs, manual_exposure) };
	}

	app_.SetControls(user_controls_);
	if (frame_limits)
	{
		libcamera::ControlList limits;
		limits.set(controls::FrameDurationLimits, libcamera::Span<const int64_t, 2>(*frame_limits));
		app_.SetControls(limits);
	}
	app_.StartCamera();
	phase_ = phase;
}

void Engine::Restart(Phase phase)
{
	Clock::time_point t = Clock::now();
	app_.StopCamera();
	if (display_)
		display_->Reset(); // the last image stays on the screen during the reconfiguration
	app_.Teardown();
	Start(phase);
	started_ = Clock::now();
	reconfigure_ms_ = ms_between(t, started_);
}

void Engine::OnFrame(CompletedRequestPtr &request)
{
	if (request->framerate > 0)
		fps_ = request->framerate;

	if (phase_ == Phase::Still)
	{
		// Switch strategy: the first full resolution frame is the photo.
		if (pending_)
			GrabStill(request);
		next_phase_ = Phase::Viewfinder;
		return;
	}
	// ZSL: the photo is the first frame completed after the command was handled.
	if (pending_ && zsl_)
		GrabStill(request);

	if (display_)
	{
		StreamInfo info;
		if (Stream *viewfinder = app_.ViewfinderStream(&info))
			display_->ShowFrame(request, viewfinder, info);
	}

	if (Clock::now() - last_stats_ >= kStatsPeriod)
		PublishStats(request);
}

void Engine::GrabStill(CompletedRequestPtr &request)
{
	PendingCapture capture = std::move(*pending_);
	pending_.reset();
	Clock::time_point t_frame = Clock::now();

	auto job = std::make_unique<CaptureJob>();
	job->id = capture.id;
	job->base_path = capture.base_path;
	job->jpeg = capture.jpeg;
	job->dng = capture.dng;
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
	job->metadata = request->metadata;
	job->options = std::make_unique<StillOptions>();
	job->options->Set() = app_.GetOptions()->Get();
	job->options->Set().quality = capture.quality;
	job->options->Set().exif = capture.exif;
	job->camera_model = app_.CameraModel();

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
	if (!zsl_)
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
		LumaStats luma =
			analyse_luma(sync.Get()[0].data(), stream_info.width, stream_info.height, stream_info.stride, step);
		event["mean_y"] = luma.mean;
		event["histogram"] = luma.histogram;
	}

	FrameInfo frame(request);
	event["exposure_us"] = frame.exposure_time;
	event["analogue_gain"] = frame.analogue_gain;
	event["digital_gain"] = frame.digital_gain;
	event["colour_gains"] = frame.colour_gains;
	event["focus_fom"] = frame.focus;
	event["ae_converged"] = frame.aelock;
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
			for (auto const &[id, value] : list)
				user_controls_.set(id, value);
			app_.SetControls(list);
		}
		else if (name == "capture")
			reply.update(Capture(command, received));
		else if (name == "configure")
		{
			std::string strategy = command.at("strategy").get<std::string>();
			if (strategy != "zsl" && strategy != "switch")
				throw CommandError("bad_request", "strategy must be \"zsl\" or \"switch\"");
			if (pending_ || phase_ == Phase::Still)
				throw CommandError("busy", "a capture is in progress");
			bool zsl = strategy == "zsl";
			if (zsl != zsl_)
			{
				zsl_ = zsl;
				next_phase_ = Phase::Viewfinder;
			}
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
	capture.quality = command.value("quality", 93);
	capture.exif = command.value("exif", std::vector<std::string> {});
	capture.t_command = received;

	uint64_t id = capture.id;
	pending_ = std::move(capture);
	if (!zsl_)
		next_phase_ = Phase::Still;
	return { { "capture_id", id } };
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
		{ "fps", fps_ },
		{ "write_queue", writer_.Queued() },
		{ "max_queue", cfg_.max_queue },
		{ "streams", streams },
		{ "controls", controls_to_json(user_controls_) },
		{ "display", display_ ? display_->Info() : json(nullptr) },
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
