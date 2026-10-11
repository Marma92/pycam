#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

#include <libcamera/controls.h>
#include <nlohmann/json.hpp>

#include "core/rpicam_app.hpp"
#include "core/still_options.hpp"

#include "capture_writer.hpp"
#include "display.hpp"
#include "focus_aids.hpp"
#include "ipc_server.hpp"
#include "look.hpp"

namespace pycam
{

struct EngineConfig
{
	std::string socket_path = "/run/pycam/engine.sock"; // /run/pycam: system/pycam.tmpfiles
	bool zsl = false; // capture strategy at startup, can be changed with "configure"
	unsigned vf_width = 1024, vf_height = 768;
	unsigned lores_width = 320, lores_height = 240;
	size_t max_queue = 3; // captures waiting to be written, ~37 MB of RAM each
	unsigned zsl_buffers = 3; // full resolution buffer sets in ZSL, ~37 MB of CMA each
	std::string zsl_denoise = "cdn_fast"; // cdn_hq divides the ZSL frame rate by 4 on a Pi 3
	std::string still_denoise = "cdn_hq"; // switch strategy: denoise of the full resolution frame
	bool display = true; // show the viewfinder on the screen (DRM), if there is one
	std::string hud_shm = "/pycam-hud"; // shared memory for the HUD, in /dev/shm
	// ISP tuning: the "pycam base" (engine/tuning, docs/presets.md). An empty folder or
	// name: libcamera's own tuning for the sensor.
	std::string tuning_dir;
	std::string tuning = "pycam-imx477";
	unsigned verbose = 0;
};

class PycamApp : public RPiCamApp
{
public:
	PycamApp() : RPiCamApp(std::make_unique<StillOptions>()) {}
	StillOptions *GetOptions() const { return static_cast<StillOptions *>(RPiCamApp::GetOptions()); }
};

// The camera loop. Everything that touches the camera happens on the thread that
// calls Run(): IPC commands are queued and executed between two frames.
//
// Two capture strategies (docs/architecture.md §2.2):
//  - switch: binned viewfinder (smooth); a capture stops the camera, switches to the
//    full resolution mode for one frame, then comes back (~0.4 s of shutter lag);
//  - zsl: the sensor runs at full resolution all the time (viewfinder ~10 fps); a
//    capture takes the next frame.
class Engine
{
public:
	explicit Engine(EngineConfig cfg);

	int Run();
	void RequestQuit(); // thread-safe

private:
	using Clock = std::chrono::steady_clock;
	using json = nlohmann::json;

	enum class Phase
	{
		Viewfinder,
		Still, // switch strategy only: full resolution mode for one frame
	};

	struct PendingCapture
	{
		uint64_t id;
		std::string base_path;
		bool jpeg;
		bool dng;
		bool compress_dng;
		int quality;
		std::vector<std::string> exif;
		std::string base_jpeg_path;
		Clock::time_point t_command;
	};

	void TryDisplay();
	void BeforeShow(CompletedRequestPtr const &request, libcamera::Stream *stream, StreamInfo const &info);
	void Start(Phase phase);
	void Restart(Phase phase);
	void Reopen();
	void SetTuning();
	void FindCameraControls();
	void OnFrame(CompletedRequestPtr &request);
	void GrabStill(CompletedRequestPtr &request);
	void PublishStats(CompletedRequestPtr &request);
	void DrainCommands();
	void HandleCommand(int client, json const &command, Clock::time_point received);
	json Capture(json const &command, Clock::time_point received);
	void Configure(json const &command);
	void Loupe(json const &command);
	void SetLook(json const &command);
	json LookState() const;
	libcamera::ControlList LoupeCrop() const;
	json LoupeState() const;
	json State() const;
	json ListControls() const;
	void PostToCameraLoop(RPiCamApp::MsgType type);

	EngineConfig cfg_;
	// Before app_: rpicam starts libcamera, which reads the tuning file, as soon as it is built.
	std::string tuning_; // name of the tuning file in use; empty: libcamera's
	PycamApp app_;
	IpcServer ipc_;
	CaptureWriter writer_;
	std::unique_ptr<Display> display_; // null without a screen
	Clock::time_point next_display_try_ {};
	bool display_failure_logged_ = false;
	FocusAids focus_aids_; // drawn into the viewfinder frames, on the display thread
	std::mutex snapshot_mutex_; // also guards preview_
	std::string snapshot_path_; // next shown frame to save as JPEG ("snapshot" command)
	std::shared_ptr<const Preview> preview_; // the look on the viewfinder, roughly; none: the base
	libcamera::ControlInfoMap const *camera_controls_ = nullptr;

	std::mutex commands_mutex_;
	std::deque<std::tuple<int, json, Clock::time_point>> commands_;

	libcamera::ControlList user_controls_; // everything set by the app, re-applied after each reconfiguration
	bool zsl_;
	std::string still_denoise_; // denoise of the photos: off, cdn_off, cdn_fast, cdn_hq
	std::optional<std::array<unsigned, 2>> still_size_; // JPEG size; none: the full sensor
	Phase phase_ = Phase::Viewfinder;
	std::optional<Phase> next_phase_; // reconfiguration to do once the current frame is released
	bool reopen_ = false; // the tuning changed: close and reopen the camera
	std::optional<PendingCapture> pending_;
	std::shared_ptr<const Look> look_ = std::make_shared<Look>(); // given to each capture
	uint64_t next_capture_id_ = 1;
	// Loupe: zoom 1 = off. Above 1, the viewfinder runs the sensor at full resolution and
	// crops around the centre (a fraction of the image), whatever the capture strategy.
	float loupe_zoom_ = 1;
	float loupe_x_ = 0.5f, loupe_y_ = 0.5f;
	std::atomic<bool> quit_ { false };

	float fps_ = 0;
	Clock::time_point last_stats_ {};
	double reconfigure_ms_ = 0; // duration of the last camera reconfiguration
	Clock::time_point started_ {}; // when the camera was last started
	unsigned frames_since_start_ = 0; // real frames received since (see Run(): deadlock)
};

} // namespace pycam
