#include "display.hpp"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <iostream>
#include <stdexcept>

#include <drm_fourcc.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

#include <libcamera/color_space.h>
#include <libcamera/framebuffer.h>

using nlohmann::json;

namespace pycam
{

namespace
{

std::runtime_error drm_error(std::string const &what)
{
	return std::runtime_error(what + ": " + std::strerror(errno));
}

bool supports(drmModePlane const *plane, uint32_t format)
{
	return std::find(plane->formats, plane->formats + plane->count_formats, format) !=
		   plane->formats + plane->count_formats;
}

std::optional<uint64_t> plane_property(int fd, uint32_t plane, char const *name)
{
	drmModeObjectProperties *props = drmModeObjectGetProperties(fd, plane, DRM_MODE_OBJECT_PLANE);
	if (!props)
		return std::nullopt;
	std::optional<uint64_t> value;
	for (uint32_t i = 0; i < props->count_props && !value; i++)
	{
		drmModePropertyRes *prop = drmModeGetProperty(fd, props->props[i]);
		if (prop && !std::strcmp(prop->name, name))
			value = props->prop_values[i];
		drmModeFreeProperty(prop);
	}
	drmModeFreeObjectProperties(props);
	return value;
}

} // namespace

Display::Display(unsigned image_width, unsigned image_height, std::string const &hud_shm_name)
	: hud_shm_name_(hud_shm_name)
{
	fd_ = drmOpen("vc4", nullptr);
	if (fd_ < 0)
		throw drm_error("cannot open the vc4 DRM device");
	try
	{
		if (!drmIsMaster(fd_))
			throw std::runtime_error("not DRM master: is a desktop session using the screen?");
		FindOutput();
		FindPlanes();

		// Fit the image in the screen, keeping its aspect ratio.
		if (uint64_t(image_width) * screen_h_ > uint64_t(screen_w_) * image_height)
		{
			image_rect_.w = screen_w_;
			image_rect_.h = (uint64_t(screen_w_) * image_height / image_width) & ~1u;
		}
		else
		{
			image_rect_.h = screen_h_;
			image_rect_.w = (uint64_t(screen_h_) * image_width / image_height) & ~1u;
		}
		image_rect_.x = (screen_w_ - image_rect_.w) / 2;
		image_rect_.y = (screen_h_ - image_rect_.h) / 2;

		CreateHudShm();
		for (auto &buffer : hud_)
			buffer = CreateDumb(screen_w_, screen_h_, DRM_FORMAT_ABGR8888); // bytes R, G, B, A
		// Straight alpha, as drawn by Pillow (the default is premultiplied).
		if (!SetEnumProperty(hud_plane_, "pixel blend mode", "Coverage"))
			std::cerr << "pycam-engine: display: cannot set straight alpha on the HUD plane\n";

		// Until the application draws its HUD: black around the image, over the console.
		FillMargins(hud_shm_, size_t(screen_w_) * 4);
		FillMargins(hud_[0].map, hud_[0].pitch);
		SetPlane(hud_plane_, hud_[0].fb, { 0, 0, screen_w_, screen_h_ }, screen_w_, screen_h_);
		hud_front_ = 0;
	}
	catch (...)
	{
		Cleanup();
		throw;
	}
	thread_ = std::thread(&Display::Loop, this);
}

Display::~Display()
{
	{
		std::lock_guard<std::mutex> lock(mutex_);
		stop_ = true;
	}
	cond_.notify_one();
	if (thread_.joinable())
		thread_.join();
	Cleanup();
}

void Display::Cleanup()
{
	// Planes off before their framebuffers go away: the console comes back.
	for (uint32_t plane : { hud_plane_, video_plane_ })
		if (plane)
			drmModeSetPlane(fd_, plane, crtc_, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
	pending_.reset();
	on_screen_.reset();
	for (auto const &[dmabuf, frame] : frame_fbs_)
		DestroyFrameFb(frame);
	frame_fbs_.clear();
	for (auto const &frame : retired_fbs_)
		DestroyFrameFb(frame);
	retired_fbs_.clear();
	for (auto &buffer : hud_)
		DestroyDumb(buffer);
	if (hud_shm_)
		munmap(hud_shm_, hud_shm_size_);
	hud_shm_ = nullptr;
	if (hud_shm_fd_ >= 0)
	{
		close(hud_shm_fd_);
		shm_unlink(hud_shm_name_.c_str());
	}
	hud_shm_fd_ = -1;
	if (fd_ >= 0)
		close(fd_);
	fd_ = -1;
}

void Display::FindOutput()
{
	drmModeRes *res = drmModeGetResources(fd_);
	if (!res)
		throw drm_error("drmModeGetResources");
	// The first connected screen whose CRTC already has a mode (set by the console).
	for (int i = 0; i < res->count_connectors && !crtc_; i++)
	{
		drmModeConnector *connector = drmModeGetConnector(fd_, res->connectors[i]);
		if (!connector)
			continue;
		if (connector->connection == DRM_MODE_CONNECTED && connector->encoder_id)
		{
			drmModeEncoder *encoder = drmModeGetEncoder(fd_, connector->encoder_id);
			if (encoder && encoder->crtc_id)
			{
				drmModeCrtc *crtc = drmModeGetCrtc(fd_, encoder->crtc_id);
				if (crtc && crtc->mode_valid)
				{
					crtc_ = crtc->crtc_id;
					screen_w_ = crtc->width;
					screen_h_ = crtc->height;
					for (int j = 0; j < res->count_crtcs; j++)
						if (res->crtcs[j] == crtc_)
							crtc_index_ = j;
				}
				drmModeFreeCrtc(crtc);
			}
			drmModeFreeEncoder(encoder);
		}
		drmModeFreeConnector(connector);
	}
	drmModeFreeResources(res);
	if (!crtc_)
		throw std::runtime_error("no active screen (is a display connected?)");
}

void Display::FindPlanes()
{
	drmModePlaneRes *res = drmModeGetPlaneResources(fd_);
	if (!res)
		throw drm_error("drmModeGetPlaneResources");
	// Free overlay planes of our CRTC, by stacking order. On the VC4 the order is fixed
	// (immutable zpos), so we take the two lowest: video, then HUD.
	std::vector<std::pair<uint64_t, uint32_t>> planes;
	for (uint32_t i = 0; i < res->count_planes; i++)
	{
		drmModePlane *plane = drmModeGetPlane(fd_, res->planes[i]);
		if (!plane)
			continue;
		if ((plane->possible_crtcs & (1u << crtc_index_)) && !plane->crtc_id &&
			supports(plane, DRM_FORMAT_YUV420) && supports(plane, DRM_FORMAT_ABGR8888) &&
			supports(plane, DRM_FORMAT_XRGB8888))
			planes.emplace_back(plane_property(fd_, plane->plane_id, "zpos").value_or(0), plane->plane_id);
		drmModeFreePlane(plane);
	}
	drmModeFreePlaneResources(res);
	if (planes.size() < 2)
		throw std::runtime_error("not enough free overlay planes");
	std::sort(planes.begin(), planes.end());
	video_plane_ = planes[0].second;
	hud_plane_ = planes[1].second;
}

void Display::CreateHudShm()
{
	hud_shm_size_ = size_t(screen_w_) * screen_h_ * 4;
	hud_shm_fd_ = shm_open(hud_shm_name_.c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0660);
	if (hud_shm_fd_ < 0)
		throw drm_error("shm_open " + hud_shm_name_);
	if (ftruncate(hud_shm_fd_, hud_shm_size_) < 0)
		throw drm_error("ftruncate " + hud_shm_name_);
	void *mem = mmap(nullptr, hud_shm_size_, PROT_READ | PROT_WRITE, MAP_SHARED, hud_shm_fd_, 0);
	if (mem == MAP_FAILED)
		throw drm_error("mmap " + hud_shm_name_);
	hud_shm_ = static_cast<uint8_t *>(mem);
	std::memset(hud_shm_, 0, hud_shm_size_); // transparent
}

void Display::FillMargins(uint8_t *pixels, size_t stride)
{
	// Opaque black (R, G, B, A = 0, 0, 0, 255) outside the image, transparent inside.
	for (unsigned y = 0; y < screen_h_; y++)
	{
		uint32_t *row = reinterpret_cast<uint32_t *>(pixels + y * stride);
		bool image_row = y >= unsigned(image_rect_.y) && y < image_rect_.y + image_rect_.h;
		for (unsigned x = 0; x < screen_w_; x++)
		{
			bool inside = image_row && x >= unsigned(image_rect_.x) && x < image_rect_.x + image_rect_.w;
			row[x] = inside ? 0 : 0xff000000; // little endian: A is the last byte
		}
	}
}

Display::DumbBuffer Display::CreateDumb(unsigned width, unsigned height, uint32_t format)
{
	DumbBuffer buffer;
	drm_mode_create_dumb create {};
	create.width = width;
	create.height = height;
	create.bpp = 32;
	if (drmIoctl(fd_, DRM_IOCTL_MODE_CREATE_DUMB, &create) < 0)
		throw drm_error("create dumb buffer");
	buffer.handle = create.handle;
	buffer.pitch = create.pitch;
	buffer.size = create.size;
	try
	{
		uint32_t handles[4] = { buffer.handle }, pitches[4] = { buffer.pitch }, offsets[4] = { 0 };
		if (drmModeAddFB2(fd_, width, height, format, handles, pitches, offsets, &buffer.fb, 0))
			throw drm_error("drmModeAddFB2");
		drm_mode_map_dumb map {};
		map.handle = buffer.handle;
		if (drmIoctl(fd_, DRM_IOCTL_MODE_MAP_DUMB, &map) < 0)
			throw drm_error("map dumb buffer");
		void *mem = mmap(nullptr, buffer.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, map.offset);
		if (mem == MAP_FAILED)
			throw drm_error("mmap dumb buffer");
		buffer.map = static_cast<uint8_t *>(mem);
		std::memset(buffer.map, 0, buffer.size);
	}
	catch (...)
	{
		DestroyDumb(buffer);
		throw;
	}
	return buffer;
}

void Display::DestroyDumb(DumbBuffer &buffer)
{
	if (buffer.map)
		munmap(buffer.map, buffer.size);
	if (buffer.fb)
		drmModeRmFB(fd_, buffer.fb);
	if (buffer.handle)
	{
		drm_mode_destroy_dumb destroy {};
		destroy.handle = buffer.handle;
		drmIoctl(fd_, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
	}
	buffer = {};
}

Display::FrameFb Display::ImportFrame(int dmabuf_fd, StreamInfo const &info)
{
	FrameFb frame;
	if (drmPrimeFDToHandle(fd_, dmabuf_fd, &frame.handle))
		throw drm_error("drmPrimeFDToHandle");
	uint32_t y_size = info.stride * info.height;
	uint32_t offsets[4] = { 0, y_size, y_size + (info.stride / 2) * (info.height / 2) };
	uint32_t pitches[4] = { info.stride, info.stride / 2, info.stride / 2 };
	uint32_t handles[4] = { frame.handle, frame.handle, frame.handle };
	if (drmModeAddFB2(fd_, info.width, info.height, DRM_FORMAT_YUV420, handles, pitches, offsets, &frame.fb, 0))
	{
		std::runtime_error error = drm_error("drmModeAddFB2 (YUV420)");
		DestroyFrameFb(frame);
		throw error;
	}
	return frame;
}

void Display::DestroyFrameFb(FrameFb const &frame)
{
	if (frame.fb)
		drmModeRmFB(fd_, frame.fb);
	if (frame.handle)
	{
		drm_gem_close close_args {};
		close_args.handle = frame.handle;
		drmIoctl(fd_, DRM_IOCTL_GEM_CLOSE, &close_args);
	}
}

bool Display::SetEnumProperty(uint32_t plane, char const *name, char const *value)
{
	// DRM enum names are long ("ITU-R BT.709 YCbCr"): match on a substring.
	drmModeObjectProperties *props = drmModeObjectGetProperties(fd_, plane, DRM_MODE_OBJECT_PLANE);
	if (!props)
		return false;
	bool done = false;
	for (uint32_t i = 0; i < props->count_props && !done; i++)
	{
		drmModePropertyRes *prop = drmModeGetProperty(fd_, props->props[i]);
		if (prop && !std::strcmp(prop->name, name) && drm_property_type_is(prop, DRM_MODE_PROP_ENUM))
		{
			for (int j = 0; j < prop->count_enums; j++)
			{
				if (std::strstr(prop->enums[j].name, value))
				{
					done = !drmModeObjectSetProperty(fd_, plane, DRM_MODE_OBJECT_PLANE, prop->prop_id,
													 prop->enums[j].value);
					break;
				}
			}
		}
		drmModeFreeProperty(prop);
	}
	drmModeFreeObjectProperties(props);
	return done;
}

void Display::SetPlane(uint32_t plane, uint32_t fb, Rect const &dest, unsigned src_w, unsigned src_h)
{
	if (drmModeSetPlane(fd_, plane, crtc_, fb, 0, dest.x, dest.y, dest.w, dest.h, 0, 0, src_w << 16, src_h << 16))
		throw drm_error("drmModeSetPlane");
}

void Display::ShowFrame(CompletedRequestPtr const &request, libcamera::Stream *stream, StreamInfo const &info)
{
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (pending_)
			frames_dropped_++;
		pending_ = Pending { request, stream, info };
	}
	cond_.notify_one();
}

void Display::Reset()
{
	std::unique_lock<std::mutex> lock(mutex_);
	reset_requested_ = true;
	cond_.notify_one();
	reset_done_.wait(lock, [this] { return !reset_requested_; });
}

void Display::CommitHud()
{
	{
		std::lock_guard<std::mutex> lock(hud_mutex_);
		// Never write into the buffer on the screen.
		int back = hud_pending_ >= 0 ? hud_pending_ : (hud_front_ == 0 ? 1 : 0);
		DumbBuffer &buffer = hud_[back];
		size_t row = size_t(screen_w_) * 4;
		for (unsigned y = 0; y < screen_h_; y++)
			std::memcpy(buffer.map + size_t(y) * buffer.pitch, hud_shm_ + y * row, row);
		hud_pending_ = back;
	}
	{
		std::lock_guard<std::mutex> lock(mutex_);
		hud_dirty_ = true;
	}
	cond_.notify_one();
}

void Display::FlipHud()
{
	std::lock_guard<std::mutex> lock(hud_mutex_);
	if (hud_pending_ < 0)
		return;
	SetPlane(hud_plane_, hud_[hud_pending_].fb, { 0, 0, screen_w_, screen_h_ }, screen_w_, screen_h_);
	hud_front_ = hud_pending_;
	hud_pending_ = -1;
}

json Display::Info() const
{
	std::lock_guard<std::mutex> lock(mutex_);
	return {
		{ "screen", { screen_w_, screen_h_ } },
		{ "image_rect", { image_rect_.x, image_rect_.y, image_rect_.w, image_rect_.h } },
		{ "hud",
		  { { "path", "/dev/shm" + hud_shm_name_ },
			{ "width", screen_w_ },
			{ "height", screen_h_ },
			{ "stride", screen_w_ * 4 },
			{ "format", "RGBA" } } },
		{ "frames_shown", frames_shown_ },
		{ "frames_dropped", frames_dropped_ },
	};
}

void Display::Loop()
{
	std::unique_lock<std::mutex> lock(mutex_);
	for (;;)
	{
		cond_.wait(lock, [this] { return stop_ || reset_requested_ || pending_ || hud_dirty_; });
		if (stop_)
			return;

		if (reset_requested_)
		{
			// The camera is stopped and its buffers are about to be freed (and their fd
			// numbers reused): forget them. The image on the screen stays, its memory is
			// kept alive by the DRM framebuffer until the next frame replaces it.
			for (auto const &[dmabuf, frame] : frame_fbs_)
				retired_fbs_.push_back(frame);
			frame_fbs_.clear();
			colour_space_set_ = false;
			CompletedRequestPtr dropped;
			if (pending_)
				dropped = std::move(pending_->request);
			pending_.reset();
			CompletedRequestPtr held = std::move(on_screen_);
			reset_requested_ = false;
			lock.unlock();
			dropped.reset(); // give the requests back outside our lock
			held.reset();
			lock.lock();
			reset_done_.notify_all();
			continue;
		}

		if (pending_)
		{
			Pending frame = std::move(*pending_);
			pending_.reset();
			lock.unlock();
			try
			{
				ShowVideo(frame);
			}
			catch (std::exception const &e)
			{
				std::cerr << "pycam-engine: display: " << e.what() << "\n";
			}
			lock.lock();
			frames_shown_++;
		}

		if (hud_dirty_)
		{
			hud_dirty_ = false;
			lock.unlock();
			try
			{
				FlipHud();
			}
			catch (std::exception const &e)
			{
				std::cerr << "pycam-engine: display: HUD: " << e.what() << "\n";
			}
			lock.lock();
		}
	}
}

void Display::ShowVideo(Pending &frame)
{
	libcamera::FrameBuffer *buffer = frame.request->buffers.at(frame.stream);
	int dmabuf = buffer->planes()[0].fd.get();
	auto it = frame_fbs_.find(dmabuf);
	if (it == frame_fbs_.end())
		it = frame_fbs_.emplace(dmabuf, ImportFrame(dmabuf, frame.info)).first;

	if (!colour_space_set_)
	{
		auto const &cs = frame.info.colour_space;
		SetEnumProperty(video_plane_, "COLOR_ENCODING", cs == libcamera::ColorSpace::Rec709 ? "709" : "601");
		SetEnumProperty(video_plane_, "COLOR_RANGE", cs == libcamera::ColorSpace::Sycc ? "full" : "limited");
		colour_space_set_ = true;
	}

	SetPlane(video_plane_, it->second.fb, image_rect_, frame.info.width, frame.info.height);

	// The previous frame has left the screen: its buffer goes back to the camera.
	on_screen_ = std::move(frame.request);
	for (auto const &old : retired_fbs_)
		DestroyFrameFb(old);
	retired_fbs_.clear();
}

} // namespace pycam
