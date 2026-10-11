#pragma once

#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include <libcamera/stream.h>
#include <nlohmann/json.hpp>

#include "core/completed_request.hpp"
#include "core/stream_info.hpp"

namespace pycam
{

// The screen, through DRM/KMS planes composited by the VC4 hardware (docs/architecture.md §4).
// Two planes, bottom to top:
//   video  the camera viewfinder stream, shown straight from the camera buffers, scaled;
//   hud    full screen RGBA layer drawn by the application into shared memory (hud_commit),
//          opaque black around the image by default, to hide the console.
// The VC4 has little line buffer memory for scaled planes: a third, scaled background plane
// made the HUD fail with ENOSPC at 1080p, hence the black margins drawn in the HUD.
// DRM calls happen on the display thread, so the camera loop never waits for a vblank.
class Display
{
public:
	// Called on the display thread with each frame about to be shown, e.g. to draw into it.
	using FrameHook = std::function<void(CompletedRequestPtr const &, libcamera::Stream *, StreamInfo const &)>;

	// image_width/height: the viewfinder stream size, to place the image on the screen.
	Display(unsigned image_width, unsigned image_height, std::string const &hud_shm_name, FrameHook hook = {});
	~Display();

	// Shows a camera frame. The latest frame wins; the display keeps the request (and so
	// the camera buffer) until the next frame replaces it on the screen.
	void ShowFrame(CompletedRequestPtr const &request, libcamera::Stream *stream, StreamInfo const &info);

	// Call after stopping the camera and before freeing its buffers: forgets the buffers
	// and gives back the frames held. The last image stays on the screen.
	void Reset();

	// Copies the HUD shared memory into a back buffer and puts it on the screen.
	// Returns once the copy is done: the application may draw the next HUD.
	void CommitHud();

	// Screen size, where the image is, and how to draw the HUD.
	nlohmann::json Info() const;

private:
	struct Rect
	{
		int x = 0, y = 0;
		unsigned w = 0, h = 0;
	};
	struct DumbBuffer
	{
		uint32_t handle = 0, fb = 0, pitch = 0;
		size_t size = 0;
		uint8_t *map = nullptr;
	};
	struct FrameFb
	{
		uint32_t handle = 0, fb = 0;
	};
	struct Pending
	{
		CompletedRequestPtr request;
		libcamera::Stream *stream;
		StreamInfo info;
	};

	void Cleanup();
	void FindOutput();
	void FindPlanes();
	void CreateHudShm();
	void FillMargins(uint8_t *pixels, size_t stride);
	DumbBuffer CreateDumb(unsigned width, unsigned height, uint32_t format);
	void DestroyDumb(DumbBuffer &buffer);
	FrameFb ImportFrame(int dmabuf_fd, StreamInfo const &info);
	void DestroyFrameFb(FrameFb const &frame);
	bool SetEnumProperty(uint32_t plane, char const *name, char const *value);
	void SetPlane(uint32_t plane, uint32_t fb, Rect const &dest, unsigned src_w, unsigned src_h);
	void Loop();
	void ShowVideo(Pending &frame);
	void FlipHud();

	int fd_ = -1;
	uint32_t crtc_ = 0;
	unsigned crtc_index_ = 0;
	unsigned screen_w_ = 0, screen_h_ = 0;
	Rect image_rect_;
	uint32_t video_plane_ = 0, hud_plane_ = 0;

	// HUD: shared memory written by the application, double-buffered on the screen.
	std::string hud_shm_name_;
	int hud_shm_fd_ = -1;
	uint8_t *hud_shm_ = nullptr;
	size_t hud_shm_size_ = 0;
	std::mutex hud_mutex_;
	DumbBuffer hud_[2];
	int hud_front_ = -1; // buffer on the screen, -1 before the first commit
	int hud_pending_ = -1; // buffer filled by CommitHud, waiting to be put on the screen

	// Shared with the display thread.
	mutable std::mutex mutex_;
	std::condition_variable cond_;
	std::condition_variable reset_done_;
	std::optional<Pending> pending_;
	bool reset_requested_ = false;
	bool hud_dirty_ = false;
	bool stop_ = false;
	uint64_t frames_shown_ = 0;
	uint64_t frames_dropped_ = 0;
	std::thread thread_;

	// Display thread only.
	FrameHook frame_hook_;
	std::map<int, FrameFb> frame_fbs_; // camera dmabuf fd -> DRM framebuffer
	std::vector<FrameFb> retired_fbs_; // from before Reset(), freed once replaced on the screen
	CompletedRequestPtr on_screen_;
	bool colour_space_set_ = false;
};

} // namespace pycam
